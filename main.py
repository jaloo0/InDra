import os
import re
import json
import warnings
import subprocess
import requests
import gspread
import time
from PIL import Image
from gtts import gTTS
from pydub import AudioSegment
from pydub.effects import speedup
from ddgs import DDGS 
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from youtube_transcript_api import YouTubeTranscriptApi

# --- CONFIGURATION ---
IMAGE_COUNT = 20
TARGET_SIZE = (1920, 1080)
DOWNLOAD_DIR = "video_images"
OUTPUT_VIDEO = "drama_final_video.mp4"
AUDIO_SPEEDUP_FACTOR = 1.25
# ⚠️ REPLACE THIS with your Google Drive Folder ID (Get it from the folder URL)
DRIVE_FOLDER_ID = "YOUR_FOLDER_ID_HERE" 

warnings.filterwarnings("ignore", category=DeprecationWarning)

def get_gcp_credentials():
    info = json.loads(os.environ['GCP_SERVICE_ACCOUNT'])
    return Credentials.from_service_account_info(info, scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ])

# --- NEW: GOOGLE DRIVE UPLOAD ---
def upload_to_drive(file_path, file_name):
    try:
        creds = get_gcp_credentials()
        drive_service = build('drive', 'v3', credentials=creds)
        
        file_metadata = {
            'name': file_name,
            'parents': [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID != "YOUR_FOLDER_ID_HERE" else []
        }
        media = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True)
        
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        # Make the file readable by anyone with the link
        drive_service.permissions().create(fileId=file.get('id'), body={'type': 'anyone', 'role': 'reader'}).execute()
        
        return file.get('webViewLink')
    except Exception as e:
        print(f"❌ Drive Upload Error: {e}")
        return None

# --- REMAINING HELPER FUNCTIONS ---
def get_yt_data(url):
    pattern = r'(?:v=|\/|be\/|embed\/)([0-9A-Za-z_-]{11})'
    match = re.search(pattern, url)
    video_id = match.group(1) if match else None
    try:
        oembed = requests.get(f"https://www.youtube.com/oembed?url={url}&format=json").json()
        title = oembed.get('title', 'YouTube Video')
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['hi', 'en'])
        script = " ".join([t['text'] for t in transcript_list])
        return title, script
    except: return None, None

def generate_audio(text, output_path):
    temp_audio = "temp_gtts.mp3"
    tts = gTTS(text=text, lang='hi')
    tts.save(temp_audio)
    audio = AudioSegment.from_file(temp_audio)
    fast_audio = speedup(audio, playback_speed=AUDIO_SPEEDUP_FACTOR)
    fast_audio.export(output_path, format="mp3")
    if os.path.exists(temp_audio): os.remove(temp_audio)

def download_images(query):
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    with DDGS() as ddgs:
        results = list(ddgs.images(query, max_results=IMAGE_COUNT + 5))
    count = 0
    for res in results:
        if count >= IMAGE_COUNT: break
        try:
            r = requests.get(res['image'], timeout=10)
            with open("temp.jpg", "wb") as f: f.write(r.content)
            with Image.open("temp.jpg") as img:
                img = img.convert("RGB").resize(TARGET_SIZE, Image.Resampling.LANCZOS)
                img.save(os.path.join(DOWNLOAD_DIR, f"img_{count}.jpg"), "JPEG")
            count += 1
        except: continue
    return count

def render_video(audio_path, output_path):
    img_files = sorted([f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.jpg')])
    cmd_dur = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', audio_path]
    duration = float(subprocess.run(cmd_dur, stdout=subprocess.PIPE, check=True).stdout)
    img_dur = duration / len(img_files)
    with open("list.txt", "w") as f:
        for img in img_files: f.write(f"file '{DOWNLOAD_DIR}/{img}'\nduration {img_dur}\n")
        f.write(f"file '{DOWNLOAD_DIR}/{img_files[-1]}'\n")
    cmd = f"ffmpeg -y -f concat -safe 0 -i list.txt -i {audio_path} -c:v libx264 -preset ultrafast -tune stillimage -vf \"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p\" -r 24 -c:a aac -shortest {output_path}"
    subprocess.run(cmd, shell=True, check=True)

def main():
    gc = gspread.authorize(get_gcp_credentials())
    sheet = gc.open_by_key("1TK9pn9ILNUGdoNSGdvfgXngVLhuXERr4JqlY9maAKsU").get_worksheet(0)
    records = sheet.get_all_records()

    for i, row in enumerate(records):
        row_num = i + 2
        if row.get('Status', '').strip() in ['', 'Pending']:
            yt_link = str(row.get('yt link', '')).strip()
            title = str(row.get('Title', '')).strip()
            script = str(row.get('Script', '')).strip()

            if yt_link and (not title or not script):
                title, script = get_yt_data(yt_link)
                if title:
                    sheet.update_cell(row_num, 1, title)
                    sheet.update_cell(row_num, 2, script[:5000])

            try:
                sheet.update_cell(row_num, 4, "Processing")
                generate_audio(script, "voice.mp3")
                download_images(title)
                render_video("voice.mp3", OUTPUT_VIDEO)
                
                # 🚀 Uploading to your Google Drive instead of Catbox
                video_url = upload_to_drive(OUTPUT_VIDEO, f"{title[:30]}.mp4")
                
                if video_url:
                    sheet.update_cell(row_num, 4, "Completed")
                    sheet.update_cell(row_num, 6, video_url) # Col F for Link
                else:
                    sheet.update_cell(row_num, 4, "Drive Error")
                
            except Exception as e:
                sheet.update_cell(row_num, 4, f"Error: {str(e)[:30]}")
            
            finally:
                # Cleanup
                if os.path.exists(DOWNLOAD_DIR):
                    for f in os.listdir(DOWNLOAD_DIR): os.remove(os.path.join(DOWNLOAD_DIR, f))
                for tmp in ["voice.mp3", "list.txt", "temp.jpg", OUTPUT_VIDEO]:
                    if os.path.exists(tmp): os.remove(tmp)

if __name__ == "__main__":
    main()
