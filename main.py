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

# --- PHASE 0: AUTHENTICATION ---
def get_gcp_credentials():
    # This reads the Secret you will set in GitHub Settings
    info = json.loads(os.environ['GCP_SERVICE_ACCOUNT'])
    creds = Credentials.from_service_account_info(info, scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ])
    return creds

# --- PHASE 1: PRECISION SCRAPING ---
def scrape_drama_update(url):
    print(f"🔎 Scraping content from: {url}")
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        
        title = soup.find('h1').text.strip() if soup.find('h1') else "Drama Update"
        content_div = soup.find('div', class_='entry-content') or soup.find('article')
        
        paragraphs = content_div.find_all('p')
        clean_lines = [p.text.strip() for p in paragraphs if len(p.text) > 45 
                       and not any(x in p.text.lower() for x in ["also read", "click here"])]
        
        return title, " ".join(clean_lines)[:2000]
    except Exception as e:
        print(f"❌ Scrape failed: {e}")
        return None, None

# --- PHASE 2: AUDIO (gTTS + Speedup) ---
def generate_audio(text, output_path):
    print(f"🎙️ Generating AI Voice (Speed: {AUDIO_SPEEDUP_FACTOR}x)...")
    temp_audio = "temp_gtts.mp3"
    tts = gTTS(text=text, lang='hi')
    tts.save(temp_audio)
    
    audio = AudioSegment.from_file(temp_audio)
    fast_audio = speedup(audio, playback_speed=AUDIO_SPEEDUP_FACTOR)
    fast_audio.export(output_path, format="mp3")
    os.remove(temp_audio)
    return output_path

# --- PHASE 3: IMAGES (DuckDuckGo + Resize) ---
def download_images(query):
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    print(f"🖼️ Downloading {IMAGE_COUNT} images for: {query}")
    
    with DDGS() as ddgs:
        results = list(ddgs.images(query, max_results=IMAGE_COUNT + 10))
    
    count = 0
    for res in results:
        if count >= IMAGE_COUNT: break
        try:
            r = requests.get(res['image'], timeout=10)
            if r.status_code == 200:
                temp_p = "temp.jpg"
                with open(temp_p, "wb") as f: f.write(r.content)
                with Image.open(temp_p) as img:
                    img = img.convert("RGB").resize(TARGET_SIZE, Image.Resampling.LANCZOS)
                    img.save(os.path.join(DOWNLOAD_DIR, f"img_{count}.jpg"), "JPEG")
                count += 1
                print(f"✅ Image {count}/{IMAGE_COUNT}")
        except: continue
    return count

# --- PHASE 4: VIDEO ASSEMBLY (FFmpeg) ---
def get_duration(path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path]
    return float(subprocess.run(cmd, stdout=subprocess.PIPE).stdout)

def render_video(audio_path, output_path):
    print("🚀 Rendering High-Speed Video...")
    img_files = sorted([f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.jpg')])
    duration = get_duration(audio_path)
    img_dur = duration / len(img_files)
    
    with open("list.txt", "w") as f:
        for img in img_files:
            f.write(f"file '{DOWNLOAD_DIR}/{img}'\nduration {img_dur}\n")
        f.write(f"file '{DOWNLOAD_DIR}/{img_files[-1]}'\n")

    cmd = f"ffmpeg -y -f concat -safe 0 -i list.txt -i {audio_path} -c:v libx264 -preset ultrafast -tune stillimage -vf \"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p\" -r 24 -c:a aac -shortest {output_path}"
    subprocess.run(cmd, shell=True)
    return output_path

# --- PHASE 5: GOOGLE DRIVE UPLOAD ---
def upload_to_drive(file_path, folder_id, creds):
    drive_service = build('drive', 'v3', credentials=creds)
    file_metadata = {'name': os.path.basename(file_path), 'parents': [folder_id]}
    media = MediaFileUpload(file_path, mimetype='video/mp4')
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

# --- MAIN WORKER LOOP ---
def main():
    creds = get_gcp_credentials()
    gc = gspread.authorize(creds)
    
    # Configuration from Sheet
    # Replace with your actual Sheet Name and Folder ID
    SHEET_NAME = "DramaBotQueue" 
    DRIVE_FOLDER_ID = "Your_Google_Drive_Folder_ID" 
    
    sheet = gc.open(SHEET_NAME).get_worksheet(0)
    records = sheet.get_all_records()

    for i, row in enumerate(records):
        row_num = i + 2
        if row.get('Status') == 'Pending':
            print(f"\n🔥 Starting Row {row_num}")
            try:
                sheet.update_cell(row_num, 3, "Processing")
                
                # Run All Phases
                title, text = scrape_drama_update(row['Source URL'])
                generate_audio(text, "voice.mp3")
                download_images(title)
                render_video("voice.mp3", OUTPUT_VIDEO)
                
                file_id = upload_to_drive(OUTPUT_VIDEO, DRIVE_FOLDER_ID, creds)
                
                sheet.update_cell(row_num, 3, "Completed")
                sheet.update_cell(row_num, 5, f"https://drive.google.com/file/d/{file_id}")
                
            except Exception as e:
                print(f"❌ Error: {e}")
                sheet.update_cell(row_num, 3, f"Error: {str(e)[:30]}")

if __name__ == "__main__":
    main()
