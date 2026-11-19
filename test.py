import os
import time
import io
import re

import pyttsx3
import speech_recognition as sr
from gpiozero import Button, LED
import threading
from queue import Queue

from PIL import Image
import cv2
import numpy as np
from picamera2 import Picamera2

from google import genai
from google.genai import types
from deep_translator import GoogleTranslator
from gtts import gTTS
import subprocess
import tempfile

import socket
import shutil
from concurrent.futures import ThreadPoolExecutor


# =========================
# HARDWARE SETUP
# =========================
led = LED(17)
button1 = Button(5)
button2 = Button(4)
exit_button = Button(2)

# =========================
# SPEECH SETUP
# =========================
recognizer = sr.Recognizer()

IMAGE_PATH = "image_to_examine/image.jpg"
'''def read_text(text):
    engine = pyttsx3.init()
    engine.setProperty("rate", 150)
    engine.say(text)
    engine.runAndWait()'''
    

'''      
def read_text(text, lang='en'):
    
    try:
        with tempfile.NamedTemporaryFile(delete=True, suffix=".mp3") as fp:
            tts = gTTS(text=text, lang=lang)
            tts.save(fp.name)

            # Start audio playback
            process = subprocess.Popen(
                ["mpg123", "-q", fp.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Monitor button while audio is playing
            while process.poll() is None:
                if exit_button.is_pressed:
                    print("Button pressed stopping speech")
                    process.terminate()
                    break
                time.sleep(0.05)

    except Exception as e:
        print(f"? Failed to speak: {e}") 
        
    
def read_text(text):
    try:
        # =========================
        # READ LANGUAGE CODE
        # =========================
        lang_file = os.path.join(os.path.dirname(__file__), "lang.txt")
        lang_code = None

        if os.path.exists(lang_file):
            with open(lang_file, "r") as f:
                code = f.read().strip().lower()
                if code:
                    lang_code = code

        # =========================
        # TRANSLATE (IF NEEDED)
        # =========================
        if lang_code:
            try:
                spoken_text = GoogleTranslator(source='en', target=lang_code).translate(text)
                tts_lang = lang_code
                print(f"Translated ({lang_code}): {spoken_text}")
            except Exception as e:
                print(f"? Translation failed, speaking English: {e}")
                spoken_text = text
                tts_lang = "en"
        else:
            spoken_text = text
            tts_lang = "en"
            print("No language set speaking English")

        # =========================
        # TEXT TO SPEECH
        # =========================
        with tempfile.NamedTemporaryFile(delete=True, suffix=".mp3") as fp:
            tts = gTTS(text=spoken_text, lang=tts_lang)
            tts.save(fp.name)

            process = subprocess.Popen(
                ["mpg123", "-q", fp.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # =========================
            # BUTTON INTERRUPT
            # =========================
            while process.poll() is None:
                if exit_button.is_pressed:
                    print("?? Button pressed stopping speech")
                    process.terminate()
                    break
                time.sleep(0.05)

    except Exception as e:
        print("? Error:", e)'''

_net_ok_until = 0.0

def is_internet_available(timeout=2, cache_seconds=15):
    """Quick connectivity check. A successful result is cached for a few
    seconds so repeated checks inside one action cost nothing."""
    global _net_ok_until
    now = time.monotonic()
    if now < _net_ok_until:
        return True
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=timeout):
            pass
        _net_ok_until = now + cache_seconds
        return True
    except OSError:
        return False

# -----------------------
# Split text into smaller chunks
# -----------------------
def split_text_into_chunks(text, max_chars=200):
    """First sentence becomes its own small chunk (starts speaking sooner);
    the rest is grouped up to max_chars."""
    sentences = [x for x in re.split(r'(?<=[.!?]) +', text) if x.strip()]
    if not sentences:
        return []
    chunks = [sentences[0].strip()]
    current = ""
    for sent in sentences[1:]:
        if len(current) + len(sent) < max_chars:
            current += sent + " "
        else:
            if current.strip():
                chunks.append(current.strip())
            current = sent + " "
    if current.strip():
        chunks.append(current.strip())
    return chunks

# -----------------------
# Offline English speech (no network, starts in milliseconds)
# -----------------------
USE_OFFLINE_TTS_FOR_ENGLISH = False  # False = Google (gTTS) voice, True = offline espeak
ESPEAK_VOICE = "en-us"
ESPEAK_SPEED = 185        # words per minute (default 175)
ESPEAK_AMPLITUDE = 150    # 0-200 (default 100)
_ESPEAK = shutil.which("espeak-ng") or shutil.which("espeak")

def _speak_offline(text):
    """Speak with espeak. Returns False if espeak isn't installed."""
    if not _ESPEAK:
        print("[INFO] espeak-ng not installed: falling back to gTTS")
        return False
    try:
        proc = subprocess.Popen(
            [_ESPEAK, "-v", ESPEAK_VOICE, "-s", str(ESPEAK_SPEED),
             "-a", str(ESPEAK_AMPLITUDE), "--stdin"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        proc.stdin.write(text.encode("utf-8"))   # via stdin: text can never be read as an option
        proc.stdin.close()
    except Exception as e:
        print("[ERROR] espeak:", e)
        return False

    while proc.poll() is None:
        if exit_button.is_pressed:
            proc.terminate()
            break
        time.sleep(0.05)
    return True

# -----------------------
# gTTS helpers (same Google voice, faster pipeline)
# -----------------------
TTS_SPEED = 1.35
_FFPLAY = shutil.which("ffplay")

def _synthesize(chunk, lang_code):
    """gTTS -> mp3 path (None on failure). Runs in worker threads, in parallel."""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            path = fp.name
        gTTS(text=chunk, lang=lang_code).save(path)

        if _FFPLAY:
            return path          # speed-up happens at playback: no re-encode

        # No ffplay installed: old behaviour (re-encode with ffmpeg)
        fast = path.replace(".mp3", "_fast.mp3")
        subprocess.run(
            ["ffmpeg", "-loglevel", "quiet", "-y", "-i", path,
             "-filter:a", f"atempo={TTS_SPEED}", fast],
            check=True
        )
        os.remove(path)
        return fast
    except Exception as e:
        print(f"TTS generation error: {e}")
        return None

def _wait_for(proc):
    """Wait for a process; True if the exit button stopped it."""
    while proc.poll() is None:
        if exit_button.is_pressed:
            proc.terminate()
            return True
        time.sleep(0.05)
    return False

def _play_mp3(path):
    """Play one clip. Returns True if the user pressed exit."""
    if _FFPLAY:
        proc = subprocess.Popen(
            [_FFPLAY, "-nodisp", "-autoexit", "-loglevel", "quiet",
             "-af", f"atempo={TTS_SPEED}", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if _wait_for(proc):
            return True
        if proc.returncode == 0:
            return False
        print("[INFO] ffplay failed, using mpg123 at normal speed")

    proc = subprocess.Popen(
        ["mpg123", "-q", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return _wait_for(proc)

# -----------------------
# Main streaming TTS function
# -----------------------
def read_text(text):
    t_start = time.perf_counter()

    lang_file = os.path.join(os.path.dirname(__file__), "lang.txt")
    lang_code = "en"
    if os.path.exists(lang_file):
        with open(lang_file, "r") as f:
            code = f.read().strip().lower()
            if code:
                lang_code = code

    print(text)

    # English: speak offline. No translate call, no gTTS, no ffmpeg.
    if lang_code == "en":
        if USE_OFFLINE_TTS_FOR_ENGLISH and _speak_offline(text):
            return

    # Everything below needs the internet (translate + gTTS)
    if not is_internet_available():
        play_audio("no_internet.mp3")
        return

    spoken_text = text
    if lang_code != "en":
        try:
            spoken_text = GoogleTranslator(source="en", target=lang_code).translate(text)
        except Exception:
            spoken_text = text
            lang_code = "en"

    # Split into chunks (first sentence alone = quickest first clip)
    chunks = split_text_into_chunks(spoken_text)
    if not chunks:
        return

    # Fetch every chunk at the same time; play them in order
    pool = ThreadPoolExecutor(max_workers=3)
    futures = [pool.submit(_synthesize, c, lang_code) for c in chunks]

    first_chunk = True
    try:
        for fut in futures:
            # wait for this chunk, but stay responsive to the exit button
            while not fut.done():
                if exit_button.is_pressed:
                    return
                time.sleep(0.02)

            path = fut.result()
            if not path:
                continue

            if first_chunk:
                print(f"[TIMING] speech started after {time.perf_counter() - t_start:.2f}s")
                first_chunk = False

            stopped = _play_mp3(path)
            if os.path.exists(path):
                os.remove(path)
            if stopped:
                return
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        for fut in futures:                      # tidy temp files of unplayed chunks
            if fut.done() and not fut.cancelled():
                leftover = fut.result()
                if leftover and os.path.exists(leftover):
                    os.remove(leftover)

def speech_to_text():
    with sr.Microphone() as source:
        read_text("Please say your command")
        recognizer.adjust_for_ambient_noise(source)
        audio = recognizer.listen(source)
    try:
        return recognizer.recognize_google(audio)
    except sr.UnknownValueError:
        read_text("Sorry, I did not understand")
        return ""
    except sr.RequestError:
        read_text("Network error")
        return ""

def wait_with_button2_check(duration):
    start = time.time()
    while time.time() - start < duration:
        if button2.is_pressed:
            return True
        if exit_button.is_pressed:
            return "exit"
        time.sleep(0.1)
    return False
    
_audio_proc = None

def play_audio(file, block=True):
    """Play a prompt clip. block=False returns immediately so the next
    step (camera, API call...) can run while the clip plays."""
    global _audio_proc
    # Stop any clip still playing so prompts never overlap
    if _audio_proc is not None and _audio_proc.poll() is None:
        _audio_proc.terminate()

    path = os.path.join("audio", file)
    print("Playing:", path)
    _audio_proc = subprocess.Popen(
        ["mpg123", "-q", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    if block:
        time.sleep(1.5)


# =========================
# STEWARD CLASS
# =========================
# =========================
# GEMINI: shared client + speed settings
# =========================
# Paste your key between the quotes. The environment variable is ignored.
GEMINI_API_KEY = "AIzaSyC8AAEdJpDA4a7ATfnwYoCMsz45mU2kZVA"
MODEL_NAME = "gemini-2.5-flash"   # 3.6-flash measured at 13-19 s; 2.5-flash at 2-4 s

_client = None
_client_lock = threading.Lock()

def get_client():
    """One client for the whole program, so the TLS connection is reused."""
    global _client
    with _client_lock:
        if _client is None:
            _client = genai.Client(
                api_key=GEMINI_API_KEY,
                http_options=types.HttpOptions(timeout=12000),  # ms; never hang forever
            )
        return _client

def _build_thinking_options():
    """Least thinking first. Which option a model accepts depends on its
    family, so we try them in order and remember the first one that works."""
    options = []
    candidates = [{"thinking_level": "minimal"},
                  {"thinking_level": "low"},
                  {"thinking_budget": 0}]
    if "2.5" in MODEL_NAME:               # 2.5 models use thinking_budget
        candidates.reverse()
    for kwargs in candidates:
        try:
            options.append(types.ThinkingConfig(**kwargs))
        except Exception:
            pass          # this SDK version doesn't know that option
    options.append(None)  # last resort: model defaults
    return options

_THINKING_OPTIONS = _build_thinking_options()
_thinking_idx = 0

def _make_config(max_tokens=None):
    thinking = _THINKING_OPTIONS[_thinking_idx]
    kwargs = {}
    if thinking is not None:
        kwargs["thinking_config"] = thinking
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens
    return types.GenerateContentConfig(**kwargs) if kwargs else None

def _downgrade_thinking(error_text):
    """If the API rejected our thinking setting, move to the next option."""
    global _thinking_idx
    low = error_text.lower()
    if "thinking" in low and ("400" in error_text or "INVALID_ARGUMENT" in error_text):
        if _thinking_idx < len(_THINKING_OPTIONS) - 1:
            _thinking_idx += 1
            print(f"[INFO] Thinking option rejected, trying option {_thinking_idx}")
            return True
    return False

def _warm_up():
    """Open the network connection at boot so the first real request is fast."""
    try:
        get_client().models.get(model=MODEL_NAME)
    except Exception as e:
        print("[WARM-UP] skipped:", e)

threading.Thread(target=_warm_up, daemon=True).start()


class STEWARD:
    def __init__(self):
        self.client = get_client()
        self.model_name = MODEL_NAME

        self.image_dir = "image_to_examine"
        os.makedirs(self.image_dir, exist_ok=True)

    # -------------------------
    # CAMERA
    # -------------------------
    def delete_existing_files(self):
        for f in os.listdir(self.image_dir):
            os.remove(os.path.join(self.image_dir, f))

    #IMAGE_PATH = "image_to_examine/image.jpg"

    def capture_image_rpicam(self):
        """Take ONE photo. Returns the file path, or None on failure."""
        os.makedirs(self.image_dir, exist_ok=True)

        cmd = [
            "rpicam-still",
            "-o", IMAGE_PATH,
            "--timeout", "600",       # was 1000; 300 gave a near-black image
            "--nopreview",
            "--awb", "auto",
            "--width", "1024",        # was 1280x720: smaller = faster upload
            "--height", "576",
            "--quality", "80",        # was 95
        ]

        try:
            subprocess.run(
                cmd, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"[SUCCESS] Image saved at: {IMAGE_PATH}")
            return IMAGE_PATH
        except subprocess.CalledProcessError as e:
            print("[ERROR] Capture failed:", e)
            return None

    def capture_image(self):
        play_audio("ready.mp3", block=False)
        '''filename = None

        picam2 = Picamera2()
        picam2.configure(
            picam2.create_still_configuration(main={"size": (640, 480)})
        )
        picam2.start()
        time.sleep(2)'''

        try:
            while True:
                #frame = picam2.capture_array()

                if button2.is_pressed:
                    #read_text("Capturing image")
                    #self.delete_existing_files()
                    #filename = os.path.join(self.image_dir, "image.jpg")
                    #cv2.imwrite(filename, frame)
                    play_audio("analyze.mp3", block=False)   # plays while we work
                    filename = self.capture_image_rpicam()
                    return filename   # None if capture failed

                if exit_button.is_pressed:
                    return None

                time.sleep(0.05)

        finally:
            print("done")
            '''picam2.stop()
            picam2.close()'''

        #return None #filename

    def load_image(self):
        path = os.path.join(self.image_dir, "image.jpg")
        # Same as the old cv2.ROTATE_90_CLOCKWISE, but in memory (no extra disk write)
        return Image.open(path).convert("RGB").rotate(-90, expand=True)

    # -------------------------
    # GEMINI HELPERS
    # -------------------------
    '''def gemini_image_prompt(self, image, prompt):
        if not is_internet_available():
            play_audio("no_internet.mp3")
            return
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[prompt, image]
        )
        return response.text.replace("*", "")'''
        
    '''def gemini_image_prompt(self, image, prompt):
        if not is_internet_available():
            play_audio("no_internet.mp3")
            return

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[prompt, image]
        )

        text = response.text
        print(f"Raw: \n {text}\n\n")

        # 1. Remove asterisks
        text = text.replace("*", "")

        # 2. Remove line breaks and tabs
        text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")

        # 3. Remove extra spaces (keep only single spaces)
        text = re.sub(r"\s+", " ", text)

        # 4. Strip leading/trailing spaces
        text = text.strip()

        return text'''
        
    def gemini_image_prompt(self, image, prompt, max_tokens=None):
        if not is_internet_available():          # cached, so effectively free
            play_audio("no_internet.mp3")
            return "No internet connection."

        max_retries = 3
        delay = 1  # seconds (was 2, doubling up to 16)
        attempt = 0
        t0 = time.perf_counter()

        while True:
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=[prompt, image],
                    config=_make_config(max_tokens)
                )

                text = response.text or ""
                if not text.strip():
                    if max_tokens:        # cap may have been eaten by thinking
                        max_tokens = None
                        continue
                    return "I could not get a description."

                # Clean text
                text = text.replace("*", "")
                text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
                text = re.sub(r"\s+", " ", text).strip()

                print(f"[TIMING] gemini: {time.perf_counter() - t0:.2f}s")
                return text

            except Exception as e:
                msg = str(e)
                print(f"[ERROR] {msg}")

                # Model rejected our thinking setting: try the next one, no delay
                if _downgrade_thinking(msg):
                    continue

                attempt += 1
                busy = any(k in msg for k in ("503", "UNAVAILABLE", "504", "DEADLINE", "timed out"))
                if busy and attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue

                play_audio("sys_ready.mp3")
                return "Server is busy. Please try again later." if busy else "Something went wrong."

    # -------------------------
    # FEATURES
    # -------------------------
    def describe_image(self):
        led.on()
        if not self.capture_image():
            led.off()
            return

        image = self.load_image()
        text = self.gemini_image_prompt(
            image,
            "Describe this image clearly for a visually impaired person.(in less than 50 words)",
            max_tokens=250
        )
        read_text(text)
        led.off()

    def start_chat(self):
        if not is_internet_available():
            play_audio("no_internet.mp3")
            return
            
        led.on()
        if not self.capture_image():
            led.off()
            return

        image = self.load_image()
        short_desc = self.gemini_image_prompt(
            image,
            "Describe this image in less than 30 words.",
            max_tokens=150
        )

        read_text(short_desc)
        play_audio("details.mp3")

        if wait_with_button2_check(2):
            detailed = self.gemini_image_prompt(
                image,
                "Describe this image in detail as a vision assistant. (in less than 100 words)",
                max_tokens=400
            )
            read_text(detailed)

        led.off()

    def detect_currency(self):
        if not is_internet_available():
            play_audio("no_internet.mp3")
            return
            
        led.on()
        if not self.capture_image():
            led.off()
            return

        image = self.load_image()
        text = self.gemini_image_prompt(
            image,
            "Detect currency and amount(example: 20 ghana cedis). If none, say 'No money detected'."
        )
        read_text(text)
        led.off()

    def translate_language(self):
        if not is_internet_available():
            play_audio("no_internet.mp3")
            return
        led.on()
        if not self.capture_image():
            led.off()
            return

        image = self.load_image()
        text = self.gemini_image_prompt(
            image,
            "Translate any visible text to English. If none, say 'No visible text detected'."
        )
        read_text(text)
        led.off()

    def extract_text_exact(self):
        if not is_internet_available():
            play_audio("no_internet.mp3")
            return
            
        led.on()
        if not self.capture_image():
            led.off()
            return

        image = self.load_image()
        text = self.gemini_image_prompt(
            image,
            "Extract the exact text in this image. Do not add words."
        )
        read_text(text)
        led.off()
        
    def summarize_text(self):
        if not is_internet_available():
            play_audio("no_internet.mp3")
            return
            
        led.on()
        if not self.capture_image():
            led.off()
            return

        image = self.load_image()
        text = self.gemini_image_prompt(
            image,
            "summarize the exact text in this image as brief as possible."
        )
        read_text(text)
        led.off()
        
    

    def chat_mode(self):
        led.on()
        read_text("AI mode activated")

        while True:
            #user_text = speech_to_text().lower()
            user_text = input(">> ").lower()

            if any(k in user_text for k in ["exit", "quit", "back"]):
                break

            if any(k in user_text for k in ["describe", "image", "see"]):
                self.start_chat()
            else:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=user_text
                )
                read_text(response.text.replace("*", ""))

        led.off()

    def ai_extract_text(self):
        led.on()
        status = self.captureImg()
        if status == 1:
            read_text("exiting...")
            led.off()
            return 
        image = self.load_image()
        self.chat_session.send_message("extract all text")
        response = self.chat_session.send_message(image)
        read_text(response.text.replace("*", ""))
        read_text("You can ask for more details or say return to exit")
        while True:
            text = speech_to_text()
            if any(k in text.lower() for k in ["exit", "back", "return"]):
                break
            response = self.chat_session.send_message(text)
            read_text(response.text.replace("*", ""))
        led.off()

# =========================
# ENTRY POINTS
# =========================
def start_chat():
    STEWARD().start_chat()

def start_plus_chat():
    STEWARD().describe_image()

def detect_currency():
    STEWARD().detect_currency()

def translate_language():
    STEWARD().translate_language()

def chatmain():
    STEWARD().chat_mode()

def ai_extract_text():
    STEWARD().ai_extract_text()

def text_extraction():
    STEWARD().extract_text_exact()
    
def summarize_text():
    STEWARD().summarize_text()

# =========================
# RUN
# =========================
#if __name__ == "__main__":
#    chat()
