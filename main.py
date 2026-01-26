import os
import re
import time
import json
import warnings
import subprocess
import requests
import gspread
from PIL import Image
from gtts import gTTS
from pydub import AudioSegment
from pydub.effects import speedup
from duckduckgo_search import DDGS
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- CONFIGURATION ---
IMAGE_COUNT = 20
TARGET_SIZE = (1920, 1080)
DOWNLOAD_DIR = "video_images"
OUTPUT_VIDEO = "drama_final_video.mp4"
AUDIO_SPEEDUP_FACTOR = 1.25
warnings.filterwarnings("ignore", category=DeprecationWarning)

def get_gcp_credentials():
    info = json.loads(os.environ['GCP_SERVICE_ACCOUNT'])
    return Credentials.from_service_account_info(info, scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ])

# --- PHASE 1: AUDIO (Direct from Script) ---
def generate_audio(text, output_path):
    print(f"🎙️ Generating AI Voice from Script...")
    temp_audio = "temp_gtts.mp3"
    # Uses gTTS for Hindi
    tts = gTTS(text=text, lang='hi')
    tts.save(temp_audio)
    
    audio = AudioSegment.from_file(temp_audio)
    # Speeding it up for engagement
    fast_audio = speedup(audio, playback_speed=AUDIO_SPEEDUP_FACTOR)
    fast_audio.export(output_path, format="mp3")
    os.remove(temp_audio)
    return output_path

# --- PHASE 2: IMAGES (Full Title Search) ---
def download_images(query):
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    # Clean query to remove characters that break DDG
    search_query = re.sub(r'[^\w\s\u0900-\u097F]', '', query)
    print(f"🖼️ Searching for Title: {search_query}")
    
    with DDGS() as ddgs:
        results = list(ddgs.images(search_query, max_results=IMAGE_COUNT + 10))
    
    count = 0
    for res in results:
        if count >= IMAGE_COUNT: break
        try:
            r = requests.get(res['image'], timeout=10)
            if r.status_code == 200:
                temp_p = "temp.jpg"
                with open(temp_p, "wb") as f: f.write(r.content)
                with Image.open(temp_p) as img:
                    # Convert to RGB and resize to 1080p
                    img = img.convert("RGB").resize(TARGET_SIZE, Image.Resampling.LANCZOS)
                    img.save(os.path.join(DOWNLOAD_DIR, f"img_{count}.jpg"), "JPEG")
                count += 1
                print(f"✅ Saved Image {count}")
        except: continue
    return count

# --- PHASE 3: FAST VIDEO ASSEMBLY ---
def get_duration(path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path]
    return float(subprocess.run(cmd, stdout=subprocess.PIPE).stdout)

def render_video(audio_path, output_path):
    img_files = sorted([f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.jpg')])
    duration = get_duration(audio_path)
    img_dur = duration / len(img_files)
    
    with open("list.txt", "w") as f:
        for img in img_files:
            f.write(f"file '{DOWNLOAD_DIR}/{img}'\nduration {img_dur}\n")
        f.write(f"file '{DOWNLOAD_DIR}/{img_files[-1]}'\n")

    # Ultra-fast FFmpeg command
    cmd = f"ffmpeg -y -f concat -safe 0 -i list.txt -i {audio_path} -c:v libx264 -preset ultrafast -tune stillimage -vf \"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p\" -r 24 -c:a aac -shortest {output_path}"
    subprocess.run(cmd, shell=True)

def upload_to_drive(file_path, folder_id, creds):
    drive_service = build('drive', 'v3', credentials=creds)
    file_metadata = {'name': os.path.basename(file_path), 'parents': [folder_id]}
    media = MediaFileUpload(file_path, mimetype='video/mp4')
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

# --- MAIN AUTOMATION LOOP ---
def main():
    # 1. Initialize Credentials
    creds = get_gcp_credentials()
    gc = gspread.authorize(creds)
    
    # 2. DEFINE THE IDs FIRST (This fixes your error)
    # Get this from your browser URL: /d/YOUR_ID_HERE/edit
    # REQUIRED: Change these to your actual details
    SPREADSHEET_ID = "1TK9pn9ILNUGdoNSGdvfgXngVLhuXERr4JqlY9maAKsU"
    DRIVE_FOLDER_ID = "1eLVLDtOpGn_6CXnt49VOVymEpNkmrldZ"
    
    print(f"📡 Connecting to Spreadsheet ID: {SPREADSHEET_ID}")
    
    try:
        # 3. Use the defined variable to open the sheet
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.get_worksheet(0)
        print("✅ Connection Successful!")
    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        return
    records = sheet.get_all_records()

    for i, row in enumerate(records):
        row_num = i + 2
        if row.get('Status') == 'Pending':
            print(f"\n🚀 Processing: {row['Title']}")
            try:
                sheet.update_cell(row_num, 3, "Processing")
                
                # 1. Voice from Script
                generate_audio(row['Script'], "voice.mp3")
                
                # 2. Images from Title
                download_images(row['Title'])
                
                # 3. Assemble
                render_video("voice.mp3", OUTPUT_VIDEO)
                
                # 4. Save to Drive
                file_id = upload_to_drive(OUTPUT_VIDEO, DRIVE_FOLDER_ID, creds)
                
                # 5. Update Sheet
                sheet.update_cell(row_num, 3, "Completed")
                sheet.update_cell(row_num, 4, f"https://drive.google.com/file/d/{file_id}")
                
                # Cleanup for next loop
                if os.path.exists(DOWNLOAD_DIR):
                    for f in os.listdir(DOWNLOAD_DIR): os.remove(os.path.join(DOWNLOAD_DIR, f))
                
            except Exception as e:
                print(f"❌ Failed: {e}")
                sheet.update_cell(row_num, 3, f"Error: {str(e)[:30]}")

if __name__ == "__main__":
    main()
