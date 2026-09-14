"""Google Drive v3 access via a service account (no OAuth consent flow at
runtime). Reads the service account's JSON key from GOOGLE_SERVICE_ACCOUNT_KEY
(the full JSON key file contents, as a string) and builds a Drive client
with google-api-python-client + google-auth.

Folder layout (see README.md for the full picture):
  root "Johnson Suarez Financials"                       -- new drops land here
    "Raw Originals (Archived)"                            -- historical only, see below
    "Source Documents (Standardized Names)"                -- renamed originals land here

IMPORTANT PLATFORM LIMITATION -- there is no automated write into "Raw
Originals (Archived)": Google Drive service accounts get ZERO storage quota
on a personal (non-Workspace) Drive, and creating ANY new file content --
files().copy(), files().create() with media, any upload -- fails with a 403
`storageQuotaExceeded` ("Service Accounts do not have storage quota"),
unconditionally, no matter which folder it targets. Google's own suggested
fixes (Shared Drives, domain-wide delegation) both require Google Workspace,
which this account doesn't have. The ONLY Drive writes a service account can
do here are metadata-only ones that touch zero new bytes: move (change
parents) and rename. So the pipeline does a single move+rename of the
original file straight into "Source Documents (Standardized Names)" -- see
move_and_rename_to_standardized() -- and does not duplicate it into "Raw
Originals (Archived)". That folder holds only what was filed there by hand
before this pipeline existed. If a genuine byte-identical archived copy ever
matters, the practical options are: upgrade to Google Workspace (unlocks
Shared Drives, which pool storage instead of relying on a user's/service
account's own quota), or have a human periodically copy files themselves.

The root folder id and the "Source Documents" folder id are known and
configurable via env vars (with the household's actual values as defaults).
"""
from __future__ import annotations

import io
import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive']

ROOT_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_ROOT_FOLDER_ID', '1Vdnu5u9doehcyNdNZJLxvD7OTaYSPx8a')
STANDARDIZED_FOLDER_ID = os.environ.get(
    'GOOGLE_DRIVE_STANDARDIZED_FOLDER_ID', '15bTjig6O9mUQ8TZ5jV1avfHSDA2LJl5r')


def get_service():
    raw_key = os.environ.get('GOOGLE_SERVICE_ACCOUNT_KEY')
    if not raw_key:
        raise RuntimeError(
            'GOOGLE_SERVICE_ACCOUNT_KEY must be set in the environment (the full service '
            'account JSON key, as a string). See README.md "Environment variables".')
    info = json.loads(raw_key)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


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
