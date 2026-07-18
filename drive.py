"""
drive.py — Resilient upload logic.
"""

import time
from googleapiclient.http import MediaFileUpload
from config import drive_service, log

def upload_to_drive(local_path: str, file_name: str, folder_id: str, progress_callback=None) -> str:
    if drive_service is None:
        raise Exception("Drive service not initialized. Check credentials.json.")

    file_metadata = {'name': file_name, 'parents': [folder_id]}
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            media = MediaFileUpload(local_path, resumable=True)
            request = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            )
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status and progress_callback:
                    progress_callback(status)
            
            return response.get('webViewLink')
            
        except Exception as e:
            if attempt < max_retries - 1:
                log.warning(f"Upload interrupted (Attempt {attempt+1}/{max_retries}): {str(e)}. Retrying...")
                time.sleep(10)
                continue
            else:
                log.error(f"Upload failed after {max_retries} attempts.")
                raise e
