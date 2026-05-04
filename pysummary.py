import sys
import re
import os
import logging
import signal
import requests
import yt_dlp
import subprocess
import time
import tempfile
import cv2
import numpy as np
from dotenv import load_dotenv
from google import genai

# Suppress the "Both GOOGLE_API_KEY and GEMINI_API_KEY are set" warning
logging.getLogger('google.auth._default').setLevel(logging.ERROR)
logging.getLogger('google.auth').setLevel(logging.ERROR)
logging.getLogger('google.genai').setLevel(logging.ERROR)
from youtube_transcript_api import YouTubeTranscriptApi as yta
from markdown_it import MarkdownIt
from weasyprint import HTML
from pptx import Presentation
from pptx.util import Inches
from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector

# Load environment variables (for API key)
load_dotenv()
# If both keys are present the google-genai library prints a noisy warning.
# We always use GEMINI_API_KEY explicitly, so remove GOOGLE_API_KEY.
os.environ.pop('GOOGLE_API_KEY', None)

# Shared genai client (created once to avoid per-call warnings)
_genai_client = None

def _get_genai_client():
    global _genai_client
    if _genai_client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            _genai_client = genai.Client(api_key=api_key)
    return _genai_client

# Global statistics
stats = {
    "ffmpeg_total_time": 0.0,
    "ffmpeg_calls": 0,
    "tokens_prompt": 0,
    "tokens_response": 0
}

# Thumbnail extraction safeguards to prevent hangs on network stalls.
FRAME_EXTRACT_TIMEOUT_SEC = 45
FRAME_EXTRACT_RETRIES = 2
YTDLP_SOCKET_TIMEOUT_SEC = 15
FFMPEG_RW_TIMEOUT_US = str(15 * 1_000_000)
GENAI_TIMEOUT_SEC = 60
GENAI_RETRIES = 2

# YouTube signed stream URLs expire after ~6 minutes; refresh before they go stale.
STREAM_URL_TTL_SEC = 240
_stream_url_cache = {}   # video_id -> (url, fetched_at_epoch)

def _get_stream_url(video_id, ydl_opts, force_refresh=False):
    """Return a valid (non-expired) stream URL, fetching a fresh one when needed."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    entry = _stream_url_cache.get(video_id)
    if entry and not force_refresh:
        cached_url, fetched_at = entry
        if time.time() - fetched_at < STREAM_URL_TTL_SEC:
            return cached_url
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        stream_url = info['url']
    _stream_url_cache[video_id] = (stream_url, time.time())
    return stream_url

def _sigalrm_timeout_handler(signum, frame):
    raise TimeoutError("Gemini request timed out")

def _call_with_timeout(timeout_sec, func, *args, **kwargs):
    if timeout_sec <= 0 or not hasattr(signal, 'setitimer'):
        return func(*args, **kwargs)

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _sigalrm_timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        return func(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

def get_video_id(input_str):
    """
    Extracts the video ID from a YouTube URL or returns the string if it's already an ID.
    """
    # Pattern to match various YouTube URL formats
    regex = r"(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^\"&?\/\s]{11})"
    match = re.search(regex, input_str)
    if match:
        return match.group(1)
    
    # If no match, check if it's a 11-character ID (common length for YT IDs)
    if len(input_str) == 11 and re.match(r"^[A-Za-z0-9_-]+$", input_str):
        return input_str
        
    return None

def get_video_chapters(video_id):
    """
    Fetch chapter info from YouTube via yt-dlp.
    Returns list of {'title', 'start_time', 'end_time'} dicts, or [].
    """
    print(f"  [Chapters] Fetching chapter data for {video_id}...")
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': False}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            source = info.get('chapters') or info.get('automatic_chapters') or []
            if source:
                label = 'chapters' if info.get('chapters') else 'automatic chapters'
                print(f"  [Chapters] Found {len(source)} {label}")
                return [
                    {
                        'title': ch.get('title', f"Chapter {i+1}"),
                        'start_time': ch.get('start_time', 0),
                        'end_time': ch.get('end_time', 0)
                    }
                    for i, ch in enumerate(source)
                ]
            print("  [Chapters] No chapters found")
            return []
    except Exception as e:
        print(f"  [Chapters] Error: {e}")
        return []

def calculate_frame_difference(img_path1, img_path2):
    """
    Compare two saved JPEG frames using HSV histogram correlation.
    Returns a difference score from 0 (identical) to 100 (completely different).
    """
    frame1 = cv2.imread(img_path1)
    frame2 = cv2.imread(img_path2)
    if frame1 is None or frame2 is None:
        return 100
    h = min(frame1.shape[0], frame2.shape[0])
    w = min(frame1.shape[1], frame2.shape[1])
    f1 = cv2.resize(frame1, (w, h))
    f2 = cv2.resize(frame2, (w, h))
    hsv1 = cv2.cvtColor(f1, cv2.COLOR_BGR2HSV)
    hsv2 = cv2.cvtColor(f2, cv2.COLOR_BGR2HSV)
    hist1 = cv2.calcHist([hsv1], [0, 1], None, [180, 256], [0, 180, 0, 256])
    hist2 = cv2.calcHist([hsv2], [0, 1], None, [180, 256], [0, 180, 0, 256])
    cv2.normalize(hist1, hist1, 0, 1, cv2.NORM_MINMAX)
    cv2.normalize(hist2, hist2, 0, 1, cv2.NORM_MINMAX)
    similarity = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
    return (1 - max(0, similarity)) * 100

def generate_summary(text, segment=False):
    """
    Generates a summary of the transcript using Gemma (via Google Generative AI).
    Supports either an API key or a local setup if configured.
    """
    client = _get_genai_client()

    if not client:
        return "Summary not generated: GEMINI_API_KEY environment variable not found."

    # Using the specific Gemma model requested: gemma-4-31b-it
    if segment:
        prompt = f"Please provide a very brief, one-sentence summary of this specific video segment transcript:\n\n{text}"
        summary_scope = "segment"
    else:
        prompt = f"Please provide a concise summary of the following YouTube transcript:\n\n{text}"
        summary_scope = "full transcript"

    last_error = None
    for attempt in range(1, GENAI_RETRIES + 1):
        try:
            response = _call_with_timeout(
                GENAI_TIMEOUT_SEC,
                client.models.generate_content,
                model='models/gemma-4-31b-it',
                contents=prompt,
            )

            # Track tokens if metadata is available
            if hasattr(response, 'usage_metadata'):
                stats["tokens_prompt"] += response.usage_metadata.prompt_token_count
                stats["tokens_response"] += response.usage_metadata.candidates_token_count

            if getattr(response, 'text', None):
                return response.text
            return "Summary not generated: empty model response."
        except TimeoutError:
            last_error = f"timed out after {GENAI_TIMEOUT_SEC}s"
            if attempt < GENAI_RETRIES:
                print(f"  [AI] {summary_scope} summary timeout ({attempt}/{GENAI_RETRIES}), retrying...")
            else:
                print(f"  [AI] {summary_scope} summary timeout after {GENAI_RETRIES} attempt(s); continuing")
        except Exception as e:
            last_error = str(e)
            if attempt < GENAI_RETRIES:
                print(f"  [AI] {summary_scope} summary error ({attempt}/{GENAI_RETRIES}): {e}; retrying...")
            else:
                print(f"  [AI] {summary_scope} summary failed after {GENAI_RETRIES} attempt(s): {e}")

    return f"Summary not generated: {last_error}"

def extract_frame(video_id, timestamp, output_path, thumb_num=None, thumb_total=None):
    """
    Extracts a single frame from the YouTube video stream at the specified timestamp.
    Optimized to use faster seek and reduce connection time.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        'format': 'bestvideo[height<=480]', # Lower resolution for faster extraction
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': YTDLP_SOCKET_TIMEOUT_SEC,
    }

    try:
        stream_url = _get_stream_url(video_id, ydl_opts)

        # Placing -ss BEFORE -i is much faster as it performs a fast seek at the stream level
        # rather than decoding everything up to that point.
        cmd = [
            'ffmpeg',
            '-rw_timeout', FFMPEG_RW_TIMEOUT_US,
            '-ss', str(timestamp),
            '-i', stream_url,
            '-vframes', '1',
            '-q:v', '5', # Slightly lower quality (5 vs 2) for faster encoding
            output_path,
            '-y',
            '-loglevel', 'error'
        ]

        # Patterns in ffmpeg stderr that indicate the stream URL has expired or the
        # connection dropped — in these cases we refresh the URL rather than retrying
        # the same stale URL.
        _CONN_ERR = ('Connection timed out', 'IO error', 'Error opening input',
                     '403', 'Forbidden', 'Error in the pull function')

        counter = f" [{thumb_num}/{thumb_total}]" if thumb_num is not None and thumb_total is not None else ""
        print(f"  [Thumbnail{counter}] Extracting frame at {_fmt_ts(timestamp)}...")

        for attempt in range(1, FRAME_EXTRACT_RETRIES + 1):
            start_time = time.time()
            try:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=FRAME_EXTRACT_TIMEOUT_SEC,
                    check=False,
                )
                duration = time.time() - start_time
                stats["ffmpeg_total_time"] += duration
                stats["ffmpeg_calls"] += 1

                if result.returncode == 0 and os.path.exists(output_path):
                    print(f"  [Thumbnail{counter}] Done in {duration:.2f}s")
                    return True

                err = result.stderr.decode('utf-8', errors='ignore')
                conn_error = any(p in err for p in _CONN_ERR)
                if attempt < FRAME_EXTRACT_RETRIES:
                    # Always force-refresh URL on retry; essential when URL expired.
                    reason = 'connection error' if conn_error else f'exit {result.returncode}'
                    print(f"  [Thumbnail{counter}] Failed ({reason}) after {duration:.2f}s; refreshing URL and retrying ({attempt + 1}/{FRAME_EXTRACT_RETRIES})...")
                    stream_url = _get_stream_url(video_id, ydl_opts, force_refresh=True)
                    cmd[6] = stream_url
                else:
                    print(f"  [Thumbnail{counter}] Failed after {duration:.2f}s (ffmpeg exit {result.returncode})")
                    if err.strip():
                        print(f"    ffmpeg: {err.strip()[:200]}")

            except subprocess.TimeoutExpired:
                duration = time.time() - start_time
                stats["ffmpeg_total_time"] += duration
                stats["ffmpeg_calls"] += 1
                if attempt < FRAME_EXTRACT_RETRIES:
                    print(f"  [Thumbnail{counter}] Timeout after {duration:.2f}s; refreshing URL and retrying ({attempt + 1}/{FRAME_EXTRACT_RETRIES})...")
                    stream_url = _get_stream_url(video_id, ydl_opts, force_refresh=True)
                    cmd[6] = stream_url
                else:
                    print(f"  [Thumbnail{counter}] Timeout after {duration:.2f}s; skipping this thumbnail")

        return False
    except Exception as e:
        print(f"Frame extraction failed for {timestamp}s: {e}")
        return False

def _fmt_ts(seconds):
    """Format seconds as [HH:MM:SS] or [MM:SS]."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"
    return f"[{minutes:02d}:{secs:02d}]"

def strip_markdown(text):
    """Remove common markdown formatting for use in PowerPoint text frames."""
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'`(.*?)`', r'\1', text)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    return text.strip()

def detect_scene_timestamps(video_id):
    """Detect scene start timestamps (in seconds) using PySceneDetect."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    temp_path = os.path.join(tempfile.gettempdir(), f"pysummary_{video_id}.mp4")

    print("  [PySceneDetect] Downloading temporary video for scene detection...")
    ydl_opts = {
        'format': 'mp4[height<=480]/best[ext=mp4]/best',
        'outtmpl': temp_path,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'overwrites': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        video = open_video(temp_path)
        fps = video.frame_rate if getattr(video, 'frame_rate', None) else 30.0
        min_scene_len_frames = max(15, int(2.0 * fps))

        manager = SceneManager()
        manager.add_detector(ContentDetector(min_scene_len=min_scene_len_frames))
        print("  [PySceneDetect] Detecting scene boundaries...")
        manager.detect_scenes(video)

        scene_list = manager.get_scene_list()
        scene_starts = []
        for start_time, _ in scene_list:
            start_seconds = int(start_time.get_seconds())
            if not scene_starts or start_seconds > scene_starts[-1]:
                scene_starts.append(start_seconds)

        print(f"  [PySceneDetect] Found {len(scene_starts)} scenes")
        return scene_starts
    except Exception as e:
        print(f"  [PySceneDetect] Error: {e}")
        return []
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

def detect_scene_timestamps_video_ai(video_id):
    """Detect shot boundaries using Google Video AI Shot Change Detection.

    Requires google-cloud-videointelligence and Application Default Credentials
    (or GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service-account JSON).
    """
    try:
        from google.cloud import videointelligence
    except ImportError:
        print("  [VideoAI] google-cloud-videointelligence is not installed.")
        print("  [VideoAI] Run: pip install google-cloud-videointelligence")
        return []

    url = f"https://www.youtube.com/watch?v={video_id}"
    temp_path = os.path.join(tempfile.gettempdir(), f"pysummary_{video_id}.mp4")

    print("  [VideoAI] Downloading temporary video for shot detection...")
    ydl_opts = {
        'format': 'mp4[height<=480]/best[ext=mp4]/best',
        'outtmpl': temp_path,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'overwrites': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        print("  [VideoAI] Sending video to Google Video Intelligence API...")
        with open(temp_path, 'rb') as f:
            input_content = f.read()

        client = videointelligence.VideoIntelligenceServiceClient()
        features = [videointelligence.Feature.SHOT_CHANGE_DETECTION]
        operation = client.annotate_video(
            request={"features": features, "input_content": input_content}
        )
        print("  [VideoAI] Waiting for shot analysis to complete (this may take a minute)...")
        result = operation.result(timeout=600)

        shot_annotations = result.annotation_results[0].shot_annotations
        scene_starts = []
        for shot in shot_annotations:
            start_sec = int(
                shot.start_time_offset.seconds
                + shot.start_time_offset.microseconds / 1_000_000
            )
            if not scene_starts or start_sec > scene_starts[-1]:
                scene_starts.append(start_sec)

        print(f"  [VideoAI] Found {len(scene_starts)} shots")
        return scene_starts
    except Exception as e:
        print(f"  [VideoAI] Error: {e}")
        return []
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

def main():
    # Parse arguments
    generate_pdf = "-pdf" in sys.argv
    generate_ppt = "-ppt" in sys.argv
    no_thumbnails = "-n" in sys.argv

    custom_name = None
    if "-o" in sys.argv:
        try:
            name_idx = sys.argv.index("-o")
            custom_name = sys.argv[name_idx + 1]
        except (ValueError, IndexError):
            pass
    elif "--output-name" in sys.argv:
        try:
            name_idx = sys.argv.index("--output-name")
            custom_name = sys.argv[name_idx + 1]
        except (ValueError, IndexError):
            pass

    thumb_interval = 90
    if "-t" in sys.argv:
        try:
            t_idx = sys.argv.index("-t")
            thumb_interval = int(sys.argv[t_idx + 1])
        except (ValueError, IndexError):
            pass

    scene_backend = "pyscenedetect"
    if "--scene-backend" in sys.argv:
        try:
            sb_idx = sys.argv.index("--scene-backend")
            scene_backend = sys.argv[sb_idx + 1].lower()
            if scene_backend not in ("pyscenedetect", "videoai"):
                print(f"Error: unknown --scene-backend '{scene_backend}'. "
                      "Choose 'pyscenedetect' or 'videoai'.")
                sys.exit(1)
        except IndexError:
            print("Error: --scene-backend requires a value (pyscenedetect or videoai)")
            sys.exit(1)

    args_to_remove = ["-pdf", "-ppt", "-n"]
    if custom_name:
        if "-o" in sys.argv:
            args_to_remove.extend(["-o", custom_name])
        if "--output-name" in sys.argv:
            args_to_remove.extend(["--output-name", custom_name])
    if "-t" in sys.argv:
        args_to_remove.extend(["-t", str(thumb_interval)])
    if "--scene-backend" in sys.argv:
        args_to_remove.extend(["--scene-backend", scene_backend])

    args = [arg for arg in sys.argv[1:] if arg not in args_to_remove]

    show_usage = not args or "--usage" in sys.argv or "--help" in sys.argv or "-h" in sys.argv
    if show_usage:
        print("""
PySummary — YouTube transcript extractor and AI summariser

Usage:
  python pysummary.py [OPTIONS] <YouTube URL or Video ID>

Options:
  -pdf                          Generate a PDF report
  -ppt                          Generate a PowerPoint presentation
  -n                            Skip thumbnail extraction (faster, text-only)
  -o, --output-name <name>      Custom base name for output files (default: video ID)
  -t <seconds>                  Fallback thumbnail interval when no chapters or
                                scene detection fails (default: 90)
  --scene-backend <backend>     Scene detection backend when no chapters found.
                                  pyscenedetect  (default) local analysis, no extra auth
                                  videoai        Google Video AI Shot Change Detection
                                                 (requires google-cloud-videointelligence
                                                  and Application Default Credentials)
  -h, --help, --usage           Show this help message

Segmentation strategy (in priority order):
  1. YouTube chapters   — used when present
  2. Scene detection    — PySceneDetect or Google Video AI (--scene-backend)
  3. Interval (-t)      — used when scene detection fails

Examples:
  python pysummary.py dQw4w9WgXcQ
  python pysummary.py -pdf -ppt dQw4w9WgXcQ
  python pysummary.py -o "My Report" -pdf dQw4w9WgXcQ
  python pysummary.py -n -t 60 dQw4w9WgXcQ
  python pysummary.py --scene-backend videoai dQw4w9WgXcQ
""")
        sys.exit(0 if (not args or "--usage" in sys.argv or "--help" in sys.argv or "-h" in sys.argv) else 1)

    input_arg = args[0]
    vid_id = get_video_id(input_arg)

    if not vid_id:
        print(f"Error: Could not extract video ID from '{input_arg}'")
        sys.exit(1)

    base_name = custom_name if custom_name else vid_id
    out_dir = base_name
    thumbs_dir = out_dir  # thumbnails go into the same output directory
    os.makedirs(out_dir, exist_ok=True)
    print(f"Output directory: {out_dir}/")

    # Fetch YouTube chapters
    chapters = get_video_chapters(vid_id)

    try:
        print(f"Fetching transcript for {vid_id}...")
        api = yta()
        transcript = api.fetch(vid_id)
        data = transcript.to_raw_data()
        print(f"Transcript fetched. Found {len(data)} segments.")

        transcript_end = data[-1]['start'] + data[-1].get('duration', 0) if data else 0

        # ------------------------------------------------------------------
        # Build segment list: each entry has start_time, end_time, title,
        # image (path or None), text (transcript dialogue for that segment)
        # ------------------------------------------------------------------
        segments = []

        if chapters:
            print(f"--- Using {len(chapters)} YouTube chapters for segmentation ---")
            for i, ch in enumerate(chapters):
                start_s = ch['start_time']
                # Use chapter end_time if valid, otherwise next chapter start or transcript end
                end_s = ch['end_time'] if ch['end_time'] and ch['end_time'] > start_s \
                    else (chapters[i + 1]['start_time'] if i + 1 < len(chapters) else transcript_end)
                segments.append({
                    'start_time': start_s,
                    'end_time': end_s,
                    'title': ch['title'],
                    'image': None,
                    'text': ''
                })
        else:
            if scene_backend == "videoai":
                print("--- No chapters. Using Google Video AI for segmentation ---")
                scene_starts = detect_scene_timestamps_video_ai(vid_id)
                backend_label = "VideoAI"
            else:
                print("--- No chapters. Using PySceneDetect for segmentation ---")
                scene_starts = detect_scene_timestamps(vid_id)
                backend_label = "PySceneDetect"

            # Fallback to interval sampling if scene detection fails.
            if not scene_starts:
                print(f"  [{backend_label}] No scenes detected. Falling back to every {thumb_interval}s.")
                last_t = -thumb_interval
                for seg in data:
                    if seg['start'] - last_t >= thumb_interval:
                        scene_starts.append(int(seg['start']))
                        last_t = seg['start']

            for i, ts in enumerate(scene_starts):
                end_ts = scene_starts[i + 1] if i + 1 < len(scene_starts) else transcript_end
                img_path = None

                if not no_thumbnails:
                    candidate_path = os.path.join(thumbs_dir, f"thumb_{int(ts)}.jpg")
                    success = extract_frame(vid_id, int(ts), candidate_path, thumb_num=i + 1, thumb_total=len(scene_starts))
                    if success:
                        img_path = candidate_path

                segments.append({
                    'start_time': ts,
                    'end_time': end_ts,
                    'title': _fmt_ts(ts),
                    'image': img_path,
                    'text': ''
                })

        # Fallback: single segment covering everything
        if not segments and data:
            segments.append({
                'start_time': data[0]['start'],
                'end_time': transcript_end,
                'title': '[00:00]',
                'image': None,
                'text': ''
            })

        # Assign transcript dialogue text to each segment
        for i, seg in enumerate(segments):
            seg_end = segments[i + 1]['start_time'] if i + 1 < len(segments) else float('inf')
            seg['text'] = ' '.join(
                s['text'].replace('\n', ' ').strip()
                for s in data
                if seg['start_time'] <= s['start'] < seg_end
            )

        # For chapter-based segments, extract thumbnails now
        if chapters and not no_thumbnails:
            print(f"Extracting {len(segments)} chapter thumbnail(s)...")
            for i, seg in enumerate(segments):
                ts = int(seg['start_time'])
                img_path = os.path.join(thumbs_dir, f"thumb_{ts}.jpg")
                if not os.path.exists(img_path):
                    success = extract_frame(vid_id, ts, img_path, thumb_num=i + 1, thumb_total=len(segments))
                    if not success:
                        img_path = None
                seg['image'] = img_path if img_path and os.path.exists(img_path) else None

        # ------------------------------------------------------------------
        # Build formatted output
        # ------------------------------------------------------------------
        main_thumbnail = f"![Thumbnail](https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg)"
        formatted_lines = []
        ppt_slides_data = []

        total_segments = len(segments)
        print(f"Processing {total_segments} segment(s)...")
        for seg_idx, seg in enumerate(segments, start=1):
            start_time = seg['start_time']
            end_time   = seg['end_time']
            start_ts_str = _fmt_ts(start_time)
            end_ts_str   = _fmt_ts(end_time)
            jump_url     = f"https://youtu.be/{vid_id}?t={int(start_time)}"
            title        = seg.get('title', start_ts_str)
            img_path     = seg.get('image')
            seg_text     = seg.get('text', '')
            print(f"[{seg_idx}/{total_segments}] {title} {start_ts_str} — {end_ts_str}")

            # Thumbnail or bold timestamp
            if img_path and os.path.exists(img_path):
                formatted_lines.append(f"![{start_ts_str}]({img_path})\n")
            else:
                formatted_lines.append(f"**{start_ts_str}**\n")

            # Section heading
            formatted_lines.append(f"### {title} {start_ts_str} — {end_ts_str} ([link]({jump_url}))\n")

            # Raw transcript dialogue for this segment
            if seg_text:
                formatted_lines.append(f"{seg_text}\n")

            # AI segment summary
            summary_segment = ''
            if seg_text:
                print(f"  Summarising segment {seg_idx}/{total_segments}...")
                summary_segment = generate_summary(seg_text, segment=True)
                formatted_lines.append(f"\n> {summary_segment}\n\n---\n")

            ppt_slides_data.append({
                "start_timestamp": start_ts_str,
                "end_timestamp":   end_ts_str,
                "start_time":      start_time,
                "end_time":        end_time,
                "title":           title,
                "image":           img_path,
                "text":            seg_text,
                "summary":         summary_segment
            })

        output_content = '\n'.join(formatted_lines)
        print("Transcript processing complete.")
        
        # Output to terminal
        print("--- Transcript ---")
        print(output_content)
        
        # Generate Summary
        print("\n--- Generating Summary ---")
        full_text = ' '.join([val['text'] for val in data])
        summary = generate_summary(full_text)
        print(summary)
        
        # Prepare Statistics Block
        stats_block = f"""
---
### Execution Statistics
- **FFmpeg Total Runtime**: {stats['ffmpeg_total_time']:.2f} seconds
- **FFmpeg Frames Extracted**: {stats['ffmpeg_calls']}
"""
        if stats['tokens_prompt'] > 0:
            stats_block += f"- **AI Tokens Used**: {stats['tokens_prompt'] + stats['tokens_response']} (Prompt: {stats['tokens_prompt']}, Response: {stats['tokens_response']})\n"
        else:
            stats_block += "- **AI Tokens Used**: N/A (Response metadata not provided)\n"

        # Output to .md file
        filename_md = os.path.join(out_dir, f"transcript_{base_name}.md")
        md_content = f"# Transcript and Summary for YouTube Video: {vid_id}\n\n" \
                     f"{main_thumbnail}\n\n" \
                     f"## Summary\n{summary}\n\n" \
                     f"## Transcript\n{output_content}\n" \
                     f"{stats_block}"
        
        with open(filename_md, "w", encoding="utf-8") as f:
            f.write(md_content)
        
        print(f"\nSuccess: Transcript and Summary saved to {filename_md}")

        # PDF Generation
        if generate_pdf:
            print("\n--- Generating PDF ---")
            filename_pdf = os.path.join(out_dir, f"transcript_{base_name}.pdf")
            
            # Use markdown-it-py to convert the EXSTING MD content to HTML
            md = MarkdownIt("commonmark", {
                "html": True,
                "linkify": True,
            })
            
            # Make image paths absolute for WeasyPrint
            cwd = os.getcwd()
            # WeasyPrint requires the base_url to resolve relative paths
            # Path conversion for thumbnails: remove relative path prefix
            # and let WeasyPrint find them via the base_url.
            md_for_pdf = md_content.replace('](' + thumbs_dir + '/', '](')
            
            html_content = md.render(md_for_pdf)
            
            # Simple CSS for the PDF
            styled_html = f"""
            <html>
                <head>
                    <style>
                        body {{ font-family: sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 40px auto; padding: 0 20px; }}
                        h1 {{ color: #1a73e8; text-align: center; }}
                        h2 {{ color: #444; border-bottom: 1px solid #ddd; padding-bottom: 10px; margin-top: 30px; }}
                        blockquote {{ background: #f9f9f9; border-left: 5px solid #ccc; margin: 1.5em 10px; padding: 0.5em 10px; font-style: italic; }}
                        img {{ max-width: 100%; height: auto; border-radius: 4px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 20px 0; display: block; }}
                        .timestamp {{ font-weight: bold; color: #555; }}
                        a {{ color: #1a73e8; text-decoration: none; }}
                        p {{ margin-bottom: 1.5em; }}
                    </style>
                </head>
                <body>
                    {html_content}
                </body>
            </html>
            """
            
            # Use the absolute path to the output directory as base_url so images resolve
            HTML(string=styled_html, base_url=os.path.join(cwd, out_dir)).write_pdf(filename_pdf)
            print(f"Success: PDF saved to {filename_pdf}")

        # PPT Generation
        if generate_ppt:
            print("\n--- Generating PowerPoint ---")
            filename_ppt = os.path.join(out_dir, f"transcript_{base_name}.pptx")
            prs = Presentation()
            
            # Download main thumbnail for title slide if possible
            main_thumb_path = os.path.join(thumbs_dir, "main_thumbnail.jpg")
            if not os.path.exists(main_thumb_path):
                r = requests.get(f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg")
                if r.status_code != 200:
                    r = requests.get(f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg")
                if r.status_code == 200:
                    with open(main_thumb_path, 'wb') as f:
                        f.write(r.content)
            
            # --- Title Slide ---
            title_slide_layout = prs.slide_layouts[0]
            slide = prs.slides.add_slide(title_slide_layout)
            title = slide.shapes.title
            subtitle = slide.placeholders[1]
            title.text = f"Video Summary: {base_name}"
            
            subtitle_text = f"YouTube Link: https://youtu.be/{vid_id}\nGenerated on: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            subtitle.text = subtitle_text
            
            if os.path.exists(main_thumb_path):
                # Add main thumbnail to the title slide
                # Position it below the subtitle
                left = Inches(2)
                top = Inches(4)
                width = Inches(6)
                slide.shapes.add_picture(main_thumb_path, left, top, width=width)

            # --- Summary Slide ---
            bullet_slide_layout = prs.slide_layouts[1]
            slide = prs.slides.add_slide(bullet_slide_layout)
            slide.shapes.title.text = "AI Generated Summary"
            body_shape = slide.shapes.placeholders[1]
            tf = body_shape.text_frame
            summary_plain = strip_markdown(summary)
            tf.text = summary_plain if len(summary_plain) < 1000 else summary_plain[:1000] + "..."
            tf.word_wrap = True

            # --- Transcript Slides ---
            from pptx.enum.text import PP_ALIGN
            from pptx.dml.color import RGBColor
            from pptx.util import Pt

            for slide_data in ppt_slides_data:
                # Use a blank layout for maximum image space
                blank_slide_layout = prs.slide_layouts[6]
                slide = prs.slides.add_slide(blank_slide_layout)

                slide_title = slide_data.get('title', '')
                has_image = bool(slide_data["image"] and os.path.exists(slide_data["image"]))

                # Slide dimensions: 10" x 7.5"
                content_left = Inches(0.5)
                img_left     = Inches(1)
                content_width = Inches(9)
                img_width    = Inches(8)

                # --- Chapter / segment title (always shown) ---
                if slide_title:
                    title_box = slide.shapes.add_textbox(content_left, Inches(0.05), content_width, Inches(0.55))
                    tf_title = title_box.text_frame
                    tf_title.text = strip_markdown(slide_title)
                    tf_title.paragraphs[0].alignment = PP_ALIGN.CENTER
                    tf_title.paragraphs[0].runs[0].font.size = Pt(20)
                    tf_title.paragraphs[0].runs[0].font.bold = True

                # --- Thumbnail image (starts below title, clear of overlap) ---
                img_top = Inches(0.65)
                if has_image:
                    slide.shapes.add_picture(slide_data["image"], img_left, img_top, width=img_width)
                else:
                    # No image: show the segment AI summary as body text
                    summary_text = strip_markdown(slide_data.get('summary', ''))
                    if summary_text:
                        body_box = slide.shapes.add_textbox(content_left, Inches(1.5), content_width, Inches(4.5))
                        tf_body = body_box.text_frame
                        tf_body.word_wrap = True
                        tf_body.text = summary_text
                        tf_body.paragraphs[0].runs[0].font.size = Pt(18)

                # --- Timestamp range (always shown, clear of slide bottom) ---
                txBox = slide.shapes.add_textbox(img_left, Inches(6.3), img_width, Inches(0.7))
                tf = txBox.text_frame
                p = tf.paragraphs[0]
                p.alignment = PP_ALIGN.CENTER

                run = p.add_run()
                run.text = "Range: "
                run.font.size = Pt(16)
                run.font.color.rgb = RGBColor(0, 0, 0)

                start_run = p.add_run()
                start_run.text = slide_data['start_timestamp']
                start_run.font.size = Pt(16)
                start_run.font.color.rgb = RGBColor(5, 99, 193)
                start_run.font.underline = True
                start_run.hyperlink.address = f"https://youtu.be/{vid_id}?t={int(slide_data['start_time'])}"

                sep_run = p.add_run()
                sep_run.text = " — "
                sep_run.font.size = Pt(16)
                sep_run.font.color.rgb = RGBColor(0, 0, 0)

                end_run = p.add_run()
                end_run.text = slide_data['end_timestamp']
                end_run.font.size = Pt(16)
                end_run.font.color.rgb = RGBColor(5, 99, 193)
                end_run.font.underline = True
                end_run.hyperlink.address = f"https://youtu.be/{vid_id}?t={int(slide_data['end_time'])}"

                # --- Speaker notes ---
                notes_slide = slide.notes_slide
                text_frame = notes_slide.notes_text_frame
                segment_summary = strip_markdown(slide_data.get('summary') or '')
                text_frame.text = (
                    f"Range: {slide_data['start_timestamp']} — {slide_data['end_timestamp']}\n\n"
                    f"Segment Summary: {segment_summary}\n\n"
                    f"Transcript: {slide_data['text']}"
                )
            
            # --- Statistics Slide ---
            stats_slide_layout = prs.slide_layouts[1]
            slide = prs.slides.add_slide(stats_slide_layout)
            slide.shapes.title.text = "Execution Statistics"
            tf = slide.shapes.placeholders[1].text_frame
            tf.text = f"FFmpeg Total Runtime: {stats['ffmpeg_total_time']:.2f} seconds"
            p = tf.add_paragraph()
            p.text = f"FFmpeg Frames Extracted: {stats['ffmpeg_calls']}"
            p = tf.add_paragraph()
            if stats['tokens_prompt'] > 0:
                p.text = f"AI Tokens Used: {stats['tokens_prompt'] + stats['tokens_response']} (Prompt: {stats['tokens_prompt']}, Response: {stats['tokens_response']})"
            else:
                p.text = "AI Tokens Used: N/A"

            prs.save(filename_ppt)
            print(f"Success: PowerPoint saved to {filename_ppt}")

        # Report Statistics to Console
        print("\n--- Execution Statistics ---")
        print(f"FFmpeg Total Runtime: {stats['ffmpeg_total_time']:.2f} seconds")
        print(f"FFmpeg Frames Extracted: {stats['ffmpeg_calls']}")
        if stats['tokens_prompt'] > 0:
            print(f"AI Tokens Used: {stats['tokens_prompt'] + stats['tokens_response']} (Prompt: {stats['tokens_prompt']}, Response: {stats['tokens_response']})")
        else:
            print("AI Tokens Used: N/A (Response metadata not provided)")

    except Exception as e:
        print(f"Error fetching transcript: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
