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

# --- FILE UPLOAD (pixeldrain → GoFile → litterbox) ---
def upload_video_file(file_path):
    """Primary: pixeldrain.com | 2nd: GoFile | 3rd: litterbox.catbox.moe"""

    filename = os.path.basename(file_path)

    # --- PRIMARY: pixeldrain.com ---
    print("📤 Uploading to pixeldrain.com...")
    try:
        with open(file_path, "rb") as f:
            response = requests.put(
                f"https://pixeldrain.com/api/file/{filename}",
                data=f,
                headers={"Content-Type": "video/mp4"},
                timeout=300
            )
        if response.status_code == 201:
            file_id = response.json().get("id")
            link = f"https://pixeldrain.com/u/{file_id}"
            print(f"✅ Pixeldrain Success: {link}")
            return link
        else:
            print(f"⚠️ Pixeldrain failed (HTTP {response.status_code}). Trying GoFile...")
    except Exception as e:
        print(f"⚠️ Pixeldrain failed: {e}. Trying GoFile...")

    # --- FALLBACK 1: GoFile (updated API endpoint) ---
    print("☁️ Uploading to GoFile...")
    try:
        with open(file_path, "rb") as f:
            response = requests.post(
                "https://store1.gofile.io/contents/uploadfile",
                files={"file": (filename, f, "video/mp4")},
                timeout=300
            ).json()
        if response.get('status') == 'ok':
            link = response['data']['downloadPage']
            print(f"✅ GoFile Success: {link}")
            return link
        else:
            print(f"⚠️ GoFile failed. Trying litterbox...")
    except Exception as e:
        print(f"⚠️ GoFile failed: {e}. Trying litterbox...")

    # --- FALLBACK 2: litterbox.catbox.moe (72h expiry) ---
    print("📦 Uploading to litterbox.catbox.moe...")
    try:
        with open(file_path, "rb") as f:
            response = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "72h"},
                files={"fileToUpload": (filename, f, "video/mp4")},
                timeout=300
            )
        if response.status_code == 200:
            link = response.text.strip()
            print(f"✅ Litterbox Success: {link}")
            return link
        else:
            print(f"❌ Litterbox failed: HTTP {response.status_code}")
    except Exception as e:
        print(f"❌ All uploaders failed: {e}")

# --- YOUTUBE INFO EXTRACTOR ---
def get_youtube_info(url):
    """Fetches title (oEmbed) and transcript (youtube_transcript_api) from a YT URL."""
    match = re.search(r'(?:v=|youtu\.be/|/v/|/embed/)([A-Za-z0-9_-]{11})', url)
    if not match:
        print(f"❌ Could not extract video ID from URL: {url}")
        return None, None
    video_id = match.group(1)
    title, script = None, None

    # Title via YouTube oEmbed — no API key needed
    try:
        resp = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=10
        )
        if resp.status_code == 200:
            title = resp.json().get('title', '')
            print(f"📺 YouTube Title: {title}")
    except Exception as e:
        print(f"⚠️ Could not get YouTube title: {e}")

    # --- BYPASS YOUTUBE BOT BLOCK via PUBLIC INVIDIOUS INSTANCES ---
    INVIDIOUS_INSTANCES = [
        "https://yewtu.be",
        "https://invidious.kavin.rocks",
        "https://inv.nadeko.net",
        "https://invidious.io.lol",
        "https://iv.melmac.space",
        "https://invidious.lunar.icu",
        "https://vid.puffyan.us",
    ]

    for instance in INVIDIOUS_INSTANCES:
        try:
            print(f"📡 Trying {instance}...")
            captions_resp = requests.get(
                f"{instance}/api/v1/captions/{video_id}",
                timeout=15
            )
            if captions_resp.status_code != 200:
                print(f"   ↳ HTTP {captions_resp.status_code}, skipping.")
                continue

            tracks = captions_resp.json()
            if not tracks:
                print(f"   ↳ No caption tracks found, skipping.")
                continue

            # Prefer Hindi/Urdu/English, else take whatever is first
            chosen = None
            for lang_prefix in ['hi', 'ur', 'en']:
                chosen = next((t for t in tracks if t.get('languageCode', '').startswith(lang_prefix)), None)
                if chosen:
                    break
            if not chosen:
                chosen = tracks[0]

            # Fetch the VTT caption file
            cap_url = chosen['url']
            if cap_url.startswith('/'):
                cap_url = instance + cap_url
            vtt_resp = requests.get(cap_url, timeout=15)
            if vtt_resp.status_code != 200:
                print(f"   ↳ Caption file HTTP {vtt_resp.status_code}, skipping.")
                continue

            # Parse VTT — remove timestamps, keep only text lines
            text_lines = []
            for line in vtt_resp.text.splitlines():
                line = line.strip()
                if not line or line.startswith('WEBVTT') or '-->' in line or line.isdigit():
                    continue
                clean = re.sub(r'<[^>]+>', '', line)
                if clean:
                    text_lines.append(clean)

            script = ' '.join(text_lines)
            print(f"✅ Captions fetched [Lang: {chosen.get('languageCode','?')}] ({len(script)} chars)")
            break

        except Exception as e:
            print(f"   ↳ Error: {e}")
            continue

    # --- LAST RESORT: YouTube's own timedtext endpoint ---
    if not script:
        print("⚠️ All Invidious failed. Trying YouTube timedtext API directly...")
        for lang in ['hi', 'en']:
            try:
                tt_resp = requests.get(
                    f"https://www.youtube.com/api/timedtext?lang={lang}&v={video_id}&fmt=vtt",
                    timeout=15,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if tt_resp.status_code == 200 and tt_resp.text.strip():
                    text_lines = []
                    for line in tt_resp.text.splitlines():
                        line = line.strip()
                        if not line or line.startswith('WEBVTT') or '-->' in line or line.isdigit():
                            continue
                        clean = re.sub(r'<[^>]+>', '', line)
                        if clean:
                            text_lines.append(clean)
                    if text_lines:
                        script = ' '.join(text_lines)
                        print(f"✅ YouTube timedtext success [lang={lang}] ({len(script)} chars)")
                        break
            except Exception as e:
                print(f"   ↳ timedtext [{lang}] failed: {e}")

    if not script:
        print("❌ All caption sources exhausted. No transcript available.")

    return title, script

# --- MAIN AUTOMATION LOOP ---
def main():
    # 1. Initialize Credentials
    creds = get_gcp_credentials()
    gc = gspread.authorize(creds)
    
    # 2. Define Spreadsheet ID
    SPREADSHEET_ID = "1TK9pn9ILNUGdoNSGdvfgXngVLhuXERr4JqlY9maAKsU"
    
    print(f"📡 Connecting to Spreadsheet ID: {SPREADSHEET_ID}")
    
    try:
        # 3. Open the sheet
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.get_worksheet(0)
        print("✅ Connection Successful!")
    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        return
    
    records = sheet.get_all_records()
    
    print(f"📊 Found {len(records)} total rows in spreadsheet")
    if len(records) > 0:
        print(f"🔍 Column names: {list(records[0].keys())}")
    
    pending_count = sum(1 for row in records if row.get('Status', '').strip() in ['', 'Pending'])
    print(f"⏳ Rows to process (empty or 'Pending' status): {pending_count}")

    for i, row in enumerate(records):
        row_num = i + 2
        # Process rows with empty status OR "Pending" status
        status = row.get('Status', '').strip()
        if status == '' or status == 'Pending':
            try:
                sheet.update_cell(row_num, 4, "Processing")

                # --- Resolve Title & Script (Sheet OR YouTube link) ---
                yt_link = row.get('yt link', '').strip()
                title  = row.get('Title', '').strip()
                script = row.get('Script', '').strip()

                if yt_link:
                    print(f"🔗 YouTube link found — fetching title & transcript...")
                    yt_title, yt_script = get_youtube_info(yt_link)
                    if yt_title:
                        title = yt_title
                    if yt_script:
                        script = yt_script

                print(f"\n🚀 Processing: {title or yt_link or '(no title)'}")

                if not script:
                    print(f"❌ No script available (no Script text and no transcript). Skipping.")
                    sheet.update_cell(row_num, 4, "No Script")
                    continue

                # 1. Voice from Script
                generate_audio(script, "voice.mp3")
                
                # 2. Images from Title
                download_images(title)
                
                # 3. Assemble Video
                render_video("voice.mp3", OUTPUT_VIDEO)
                
                # 4. Upload video (0x0.st → catbox fallback)
                video_url = upload_video_file(OUTPUT_VIDEO)
                
                if video_url:
                    # 5. Update Sheet
                    sheet.update_cell(row_num, 4, "Completed")
                    sheet.update_cell(row_num, 6, video_url)
                    print(f"✅ Updated sheet with Drive link: {video_url}")
                else:
                    sheet.update_cell(row_num, 4, "Upload Failed")
                
                # Cleanup for next loop
                if os.path.exists(DOWNLOAD_DIR):
                    for f in os.listdir(DOWNLOAD_DIR): 
                        os.remove(os.path.join(DOWNLOAD_DIR, f))
                
                # Clean up temp files
                if os.path.exists("voice.mp3"): os.remove("voice.mp3")
                if os.path.exists("list.txt"): os.remove("list.txt")
                if os.path.exists(OUTPUT_VIDEO): os.remove(OUTPUT_VIDEO)
                
            except Exception as e:
                import traceback
                error_msg = traceback.format_exc()
                print(f"❌ Failed processing '{row['Title']}':")
                print(error_msg)
                sheet.update_cell(row_num, 4, f"Error: {str(e)[:50]}")

if __name__ == "__main__":
    main()
