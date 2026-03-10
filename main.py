import os
import time
import json
import wave
import warnings
import subprocess
import requests
import gspread
from PIL import Image
from duckduckgo_search import DDGS
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- CONFIGURATION ---
IMAGE_COUNT = 20
TARGET_SIZE = (1920, 1080)
DOWNLOAD_DIR = "video_images"
OUTPUT_VIDEO = "drama_final_video.mp4"
PIPER_SPEED   = 1.25            # >1 = faster speech (same as old AUDIO_SPEEDUP_FACTOR)
PIPER_MODEL_DIR  = "piper_model"  # never cleaned up between iterations
PIPER_MODEL_NAME = "hi_IN-harini-medium"
warnings.filterwarnings("ignore", category=DeprecationWarning)

def get_gcp_credentials():
    info = json.loads(os.environ['GCP_SERVICE_ACCOUNT'])
    return Credentials.from_service_account_info(info, scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ])

# --- PHASE 1: AUDIO (Piper TTS — offline, no API needed) ---
def generate_audio(text, output_path):
    """Synthesise Hindi speech with Piper TTS.
    Model is downloaded once into piper_model/ and reused across all iterations.
    That directory is intentionally excluded from the per-iteration cleanup."""
    from piper import PiperVoice

    print("🎙️ Generating AI Voice with Piper TTS...")

    onnx_path   = os.path.join(PIPER_MODEL_DIR, f"{PIPER_MODEL_NAME}.onnx")
    config_path = os.path.join(PIPER_MODEL_DIR, f"{PIPER_MODEL_NAME}.onnx.json")

    # Download model files if not already present (once per workflow run)
    if not os.path.exists(onnx_path):
        os.makedirs(PIPER_MODEL_DIR, exist_ok=True)
        hf_base = (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "hi/hi_IN/harini/medium/" + PIPER_MODEL_NAME
        )
        for ext in [".onnx", ".onnx.json"]:
            dest = os.path.join(PIPER_MODEL_DIR, PIPER_MODEL_NAME + ext)
            print(f"⬇️  Downloading {os.path.basename(dest)}...")
            r = requests.get(hf_base + ext, stream=True, timeout=180)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        print("✅ Piper model ready.")

    voice = PiperVoice.load(onnx_path, config_path=config_path, use_cuda=False)

    # length_scale < 1 speeds up speech (inverse of PIPER_SPEED)
    with wave.open(output_path, "wb") as wav_file:
        voice.synthesize(text, wav_file, length_scale=round(1.0 / PIPER_SPEED, 3))

    print(f"✅ Audio generated: {output_path}")
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

                # --- Read Title & Script directly from sheet ---
                title  = row.get('Title', '').strip()
                script = row.get('Script', '').strip()

                print(f"\n🚀 Processing: {title or '(no title)'}")

                if not script:
                    print(f"❌ No script available (no Script text and no transcript). Skipping.")
                    sheet.update_cell(row_num, 4, "No Script")
                    continue

                # 1. Voice from Script
                generate_audio(script, "voice.wav")
                
                # 2. Images from Title
                download_images(title)
                
                # 3. Assemble Video
                render_video("voice.wav", OUTPUT_VIDEO)
                
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
                
                # Clean up temp files (piper_model/ is intentionally kept)
                if os.path.exists("voice.wav"): os.remove("voice.wav")
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
