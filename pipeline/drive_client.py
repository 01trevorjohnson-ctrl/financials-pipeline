"""Google Drive v3 access via a service account (no OAuth consent flow at
runtime). Reads the service account's JSON key from GOOGLE_SERVICE_ACCOUNT_KEY
(the full JSON key file contents, as a string) and builds a Drive client
with google-api-python-client + google-auth.

Folder layout (see README.md for the full picture):
  root "Johnson Suarez Financials"                       -- new drops land here
    "Raw Originals (Archived)"                            -- untouched copies
    "Source Documents (Standardized Names)"                -- renamed copies

The root folder id and the "Source Documents" folder id are known and
configurable via env vars (with the household's actual values as defaults).
The "Raw Originals (Archived)" folder id is resolved BY NAME under the root
at runtime and cached for the life of the process -- this was deliberate:
at dev time the read-only Drive inspection MCP connector available to this
coding session had a stale/empty search index for this folder (a known lag
issue called out in the task brief), so its id could not be read directly.
Resolving by name at runtime sidesteps that entirely and is arguably more
robust than hardcoding an id anyway (it keeps working if the folder is ever
recreated). Set GOOGLE_DRIVE_RAW_ORIGINALS_FOLDER_ID to skip the lookup.
"""
from __future__ import annotations

import io
import json
import os
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive']

ROOT_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_ROOT_FOLDER_ID', '1Vdnu5u9doehcyNdNZJLxvD7OTaYSPx8a')
STANDARDIZED_FOLDER_ID = os.environ.get(
    'GOOGLE_DRIVE_STANDARDIZED_FOLDER_ID', '15bTjig6O9mUQ8TZ5jV1avfHSDA2LJl5r')
RAW_ORIGINALS_FOLDER_NAME = 'Raw Originals (Archived)'
_raw_originals_folder_id_cache: Optional[str] = None


def get_service():
    raw_key = os.environ.get('GOOGLE_SERVICE_ACCOUNT_KEY')
    if not raw_key:
        raise RuntimeError(
            'GOOGLE_SERVICE_ACCOUNT_KEY must be set in the environment (the full service '
            'account JSON key, as a string). See README.md "Environment variables".')
    info = json.loads(raw_key)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def get_raw_originals_folder_id(service) -> str:
    global _raw_originals_folder_id_cache
    env_override = os.environ.get('GOOGLE_DRIVE_RAW_ORIGINALS_FOLDER_ID')
    if env_override:
        return env_override
    if _raw_originals_folder_id_cache:
        return _raw_originals_folder_id_cache

    q = (f"'{ROOT_FOLDER_ID}' in parents and mimeType = 'application/vnd.google-apps.folder' "
         f"and name = '{RAW_ORIGINALS_FOLDER_NAME}' and trashed = false")
    resp = service.files().list(q=q, fields='files(id, name)', pageSize=5).execute()
    files = resp.get('files', [])
    if not files:
        raise RuntimeError(
            f'Could not find a "{RAW_ORIGINALS_FOLDER_NAME}" subfolder under the root Drive '
            'folder. Confirm it exists and the service account has access to it.')
    _raw_originals_folder_id_cache = files[0]['id']
    return _raw_originals_folder_id_cache


def list_root_files(service) -> list:
    """Files directly in the root folder (not subfolders, not trashed).
    These are the "still to do" statements dropped by a household member."""
    files = []
    page_token = None
    q = f"'{ROOT_FOLDER_ID}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    while True:
        resp = service.files().list(
            q=q, fields='nextPageToken, files(id, name, mimeType, size, createdTime)',
            pageSize=100, pageToken=page_token).execute()
        files.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return files


def download_file(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def copy_to_raw_originals(service, file_id: str, original_filename: str) -> str:
    """Copy the original (untouched, under its original name) into
    "Raw Originals (Archived)". Returns the new file's id."""
    raw_folder_id = get_raw_originals_folder_id(service)
    body = {'name': original_filename, 'parents': [raw_folder_id]}
    copied = service.files().copy(fileId=file_id, body=body, fields='id').execute()
    return copied['id']


def move_and_rename_to_standardized(service, file_id: str, standardized_filename: str) -> None:
    """Move the (now-processed) original out of the root and into
    "Source Documents (Standardized Names)", renaming it on the way."""
    service.files().update(
        fileId=file_id,
        addParents=STANDARDIZED_FOLDER_ID,
        removeParents=ROOT_FOLDER_ID,
        body={'name': standardized_filename},
        fields='id, parents, name',
    ).execute()
