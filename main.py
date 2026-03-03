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
from youtube_transcript_api import YouTubeTranscriptApi

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

# --- NEW: YOUTUBE DATA HELPERS ---
def get_yt_video_id(url):
    """Extracts the 11-character video ID from various YouTube URL formats."""
    pattern = r'(?:v=|\/|be\/|embed\/)([0-9A-Za-z_-]{11})'
    match = re.search(pattern, url)
    return match.group(1) if match else None

def get_yt_data(url):
    """Fetches video title and transcript from YouTube."""
    video_id = get_yt_video_id(url)
    if not video_id:
        return None, None
    
    # 1. Fetch Title via oEmbed
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        title = requests.get(oembed_url).json().get('title', 'YouTube Video')
    except:
        title = "YouTube Video"

    # 2. Fetch Transcript (prefers Hindi, falls back to English)
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['hi', 'en'])
        script = " ".join([t['text'] for t in transcript_list])
        return title, script
    except Exception as e:
        print(f"⚠️ Could not fetch transcript for {video_id}: {e}")
        return title, None

# --- PHASE 1: AUDIO ---
def generate_audio(text, output_path):
    print(f"🎙️ Generating AI Voice from Script...")
    temp_audio = "temp_gtts.mp3"
    tts = gTTS(text=text, lang='hi')
    tts.save(temp_audio)
    
    audio = AudioSegment.from_file(temp_audio)
    fast_audio = speedup(audio, playback_speed=AUDIO_SPEEDUP_FACTOR)
    fast_audio.export(output_path, format="mp3")
    os.remove(temp_audio)
    return output_path

# --- PHASE 2: IMAGES ---
def download_images(query):
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
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
                    img = img.convert("RGB").resize(TARGET_SIZE, Image.Resampling.LANCZOS)
                    img.save(os.path.join(DOWNLOAD_DIR, f"img_{count}.jpg"), "JPEG")
                count += 1
                print(f"✅ Saved Image {count}")
        except: continue
    return count

# --- PHASE 3: VIDEO ASSEMBLY ---
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

    cmd = f"ffmpeg -y -f concat -safe 0 -i list.txt -i {audio_path} -c:v libx264 -preset ultrafast -tune stillimage -vf \"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p\" -r 24 -c:a aac -shortest {output_path}"
    subprocess.run(cmd, shell=True)

def upload_to_gofile(file_path):
    print("☁️ Uploading Video...")
    try:
        server_resp = requests.get("https://api.gofile.io/getServer").json()
        server = server_resp['data']['server'] if server_resp['status'] == 'ok' else server_resp['data']['serversAllZone'][0]['name']
        
        upload_url = f"https://{server}.gofile.io/uploadFile"
        with open(file_path, "rb") as f:
            response = requests.post(upload_url, files={"file": f}).json()
        
        if response['status'] == 'ok':
            return response['data']['downloadPage']
    except:
        pass
    
    try:
        catbox_url = "https://catbox.moe/user/api.php"
        with open(file_path, "rb") as f:
            response = requests.post(catbox_url, data={"reqtype": "fileupload"}, files={"fileToUpload": f})
        if response.status_code == 200:
            return response.text
    except:
        return None

# --- MAIN AUTOMATION LOOP ---
def main():
    creds = get_gcp_credentials()
    gc = gspread.authorize(creds)
    SPREADSHEET_ID = "1TK9pn9ILNUGdoNSGdvfgXngVLhuXERr4JqlY9maAKsU" #
    
    try:
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.get_worksheet(0)
    except Exception as e:
        print(f"❌ Spreadsheet Connection Failed: {e}")
        return
    
    records = sheet.get_all_records()
    
    for i, row in enumerate(records):
        row_num = i + 2
        status = row.get('Status', '').strip()
        
        if status in ['', 'Pending']:
            title = str(row.get('Title', '')).strip()
            script_text = str(row.get('Script', '')).strip()
            yt_link = str(row.get('YT Link', '')).strip()

            # --- LOGIC: Use YouTube Link if Provided ---
            if yt_link and (not title or not script_text):
                print(f"\n🔗 Extracting from YouTube: {yt_link}")
                yt_title, yt_script = get_yt_data(yt_link)
                
                if yt_script:
                    title = yt_title
                    script_text = yt_script
                    # Update sheet with extracted info
                    sheet.update_cell(row_num, 1, title) # Column A
                    sheet.update_cell(row_num, 2, script_text[:5000]) # Column B
                else:
                    sheet.update_cell(row_num, 4, "Error: No Transcript") # Column D
                    continue

            if not title or not script_text:
                continue

            print(f"\n🚀 Processing: {title}")
            try:
                sheet.update_cell(row_num, 4, "Processing") # Status is now Column D
                
                generate_audio(script_text, "voice.mp3")
                download_images(title)
                render_video("voice.mp3", OUTPUT_VIDEO)
                
                video_url = upload_to_gofile(OUTPUT_VIDEO)
                
                if video_url:
                    sheet.update_cell(row_num, 4, "Completed") # Column D
                    sheet.update_cell(row_num, 5, video_url) # Video Link is now Column E
                else:
                    sheet.update_cell(row_num, 4, "Upload Failed")
                
                # Cleanup
                if os.path.exists(DOWNLOAD_DIR):
                    for f in os.listdir(DOWNLOAD_DIR): os.remove(os.path.join(DOWNLOAD_DIR, f))
                for tmp in ["voice.mp3", "list.txt", OUTPUT_VIDEO]:
                    if os.path.exists(tmp): os.remove(tmp)
                
            except Exception as e:
                print(f"❌ Failed: {e}")
                sheet.update_cell(row_num, 4, f"Error: {str(e)[:50]}")

if __name__ == "__main__":
    main()
