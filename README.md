# PySummary

PySummary extracts YouTube transcripts, segments the video timeline, generates AI summaries, and exports rich reports in Markdown, PDF, and PowerPoint.

## Features
- Transcript extraction from YouTube URL or video ID
- AI summary generation with Google Gemini
- Multi-format export: Markdown, PDF, PowerPoint
- Chapter-aware workflow:
  - If chapters exist, PySummary uses them as segment boundaries
  - If chapters do not exist, PySummary uses PySceneDetect scene boundaries
  - If scene detection fails, it falls back to interval segmentation via `-t`
- Timestamped links back to YouTube
- Per-segment slide notes with transcript text and short AI summary
- Markdown stripped from PowerPoint text frames (bold, italic, headers, bullets)
- Slides always show title and timestamp range, even when no thumbnail is available
- Optional custom output naming via `-o`/`--output-name`
- Optional thumbnail skip mode via `-n`
- Thumbnail extraction starts from the first chapter/scene timestamp; timed-out frames are skipped automatically
- Gemini API requests are guarded by a hard timeout with automatic retry so stalled network calls never hang the full run

## How Segmentation Works

PySummary builds timeline segments in this order of priority:

1. YouTube chapters
2. PySceneDetect scene boundaries
3. Fixed interval fallback (`-t`)

### 1) Chapter-Based Segmentation (Preferred)

If the video metadata includes chapters (manual or automatic), PySummary uses each chapter start/end as segment boundaries.

- Why this is preferred:
   - Chapters usually match the creator's intended topic structure.
   - Segment titles come from chapter titles.
   - Thumbnails are extracted at chapter starts.

### 2) PySceneDetect Segmentation (No Chapters)

If chapters are not present, PySummary runs PySceneDetect to find visual scene changes.

- What it does:
   - Downloads a temporary lower-resolution video file.
   - Detects scene boundaries using content-change analysis.
   - Uses each detected scene start time as a segment boundary.
   - Extracts thumbnails at those scene starts.

- Why this helps:
   - Segments follow visual transitions in the video.
   - Produces more meaningful segment boundaries than fixed-time slicing.

### 3) `-t` Fallback Interval

If PySceneDetect cannot produce usable scene boundaries, PySummary falls back to fixed interval segmentation.

- `-t <seconds>` sets this fallback interval.
- Default is `90` seconds.
- Example:
   ```bash
   python pysummary.py -t 60 dQw4w9WgXcQ
   ```

In short: chapters are used when available, PySceneDetect is used when chapters are missing, and `-t` is the safety net if scene detection is unavailable or fails.

## Installation

### Prerequisites
- Python 3.12+
- FFmpeg
- Gemini API key (`GEMINI_API_KEY`)
- Ubuntu libraries for WeasyPrint:
  - `sudo apt install -y libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0`

### Setup
1. Install FFmpeg:
   ```bash
   sudo apt update && sudo apt install -y ffmpeg
   ```
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure API key:
   ```bash
   export GEMINI_API_KEY="your-api-key-here"
   ```

## Usage & Examples

### Basic Markdown Output
```bash
python pysummary.py dQw4w9WgXcQ
```

### PDF Output
```bash
python pysummary.py -pdf dQw4w9WgXcQ
```

### PowerPoint Output
```bash
python pysummary.py -ppt dQw4w9WgXcQ
```

### PDF + PPT Together
```bash
python pysummary.py -pdf -ppt dQw4w9WgXcQ
```

### Custom Output Name
```bash
python pysummary.py -o "Never Gonna Give You Up" dQw4w9WgXcQ
```

### Scene/Interval Control for No-Chapter Videos
`-t` sets the fallback interval in seconds when scene detection cannot produce segment boundaries.
```bash
python pysummary.py -t 60 dQw4w9WgXcQ
```

### Skip Thumbnail Extraction
```bash
python pysummary.py -n dQw4w9WgXcQ
```

### Full Example
```bash
python pysummary.py -pdf -ppt -o "My Full Report" -t 120 dQw4w9WgXcQ
```

## Command-Line Options
- `video_id_or_url`: YouTube video ID, full URL (`https://www.youtube.com/watch?v=…`), or short URL (`https://youtu.be/…`)
- `-pdf`: generate PDF output
- `-ppt`: generate PowerPoint output
- `-n`: skip thumbnail extraction
- `-o <name>`, `--output-name <name>`: custom output base name
- `-t <seconds>`: fallback interval (no chapters and no scenes)
- `-h`, `--help`, `--usage`: show usage information

## Outputs

All output files are written to a single directory named after the video ID or custom name (`-o`).

- `<name>/transcript_<name>.md`
- `<name>/transcript_<name>.pdf` (when `-pdf` is used)
- `<name>/transcript_<name>.pptx` (when `-ppt` is used)
- `<name>/thumbs/` — per-segment thumbnails (unless `-n`)
  - `thumb_<timestamp>.jpg` — FFmpeg-extracted frame for each chapter/scene start

## Future Work

### Scene Detection Backends

PySummary currently uses PySceneDetect for scene boundary detection. The following alternatives could offer improvements in speed, accuracy, or flexibility:

#### FFmpeg Built-in Scene Detection
FFmpeg has a native scene filter (`select='gt(scene,THRESH)'`) that can detect cuts without any extra Python dependencies. It is significantly faster than downloading and analysing a video in Python, making it a good candidate for a lightweight default backend.

#### OpenCV Custom Detector
A fully custom detector using frame-difference metrics, HSV histogram comparison, and SSIM could replace PySceneDetect entirely. This gives complete control over thresholds and avoids a third-party ML dependency. PySummary already uses OpenCV, so this would add no new requirements.

#### TransNetV2
A deep-learning shot-boundary detector (TensorFlow/PyTorch) that consistently outperforms rule-based methods on hard cuts, fades, and dissolves. Best used as an "accuracy mode" for longer, professionally edited videos.

#### Cloud Vision APIs
Google Video AI and AWS Rekognition both provide managed shot detection. High quality with no local compute required, but add cost and network/privacy constraints — suitable for production deployments.

A future `--scene-backend` option could allow users to choose between these approaches:
```bash
python pysummary.py --scene-backend ffmpeg dQw4w9WgXcQ
python pysummary.py --scene-backend transnet dQw4w9WgXcQ
```

## License
MIT
