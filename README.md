# PySummary

A tool for processing YouTube transcripts that produces timestamped reports, AI-generated summaries, and visual thumbnails across multiple file formats.

## Features
-   **Transcript Processing**: Retrieves transcripts from YouTube URLs, shortened links, or video IDs.
-   **Summarization**: Generates summaries using the Google Gemini API.
-   **Multi-format Export**: Supports Markdown, PDF (via WeasyPrint), and PowerPoint (via python-pptx) output.
-   **Automated Frame Extraction**: Uses FFmpeg and yt-dlp to extract video frames at regular intervals (approximately every 60 seconds).
-   **Interactive Timestamps**: Includes direct links to specific video moments in Markdown and PDF reports.
-   **PowerPoint Integration**: Creates slides with extracted imagery, speaker notes containing segment text, and clickable timestamp ranges.
-   **Customization**: Supports user-defined filenames and the ability to skip initial thumbnails.
-   **Technical Metrics**: Provides data on FFmpeg execution time and AI token consumption.
-   **Content Formatting**: Consolidates layout by removing excessive whitespace and newlines from the source transcript.

## Installation

### Prerequisites
-   Python 3.12+
-   **FFmpeg**: Required for precise frame extraction.
-   A Google Gemini API Key (for Gemma v4 summarization).
-   **System Libraries for WeasyPrint**: (On Ubuntu) `sudo apt install -y libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0`

### Setup
1.  Clone the repository or download the script.
2.  Install FFmpeg (on Ubuntu/Linux):
    ```bash
    sudo apt update && sudo apt install -y ffmpeg
    ```
3.  Install Python dependencies:
    ```bash
    pip install google-genai youtube-transcript-api yt-dlp markdown-it-py WeasyPrint python-pptx requests
    ```
4.  Set your Gemini API Key as an environment variable:
    ```bash
    export GEMINI_API_KEY="your-api-key-here"
    ```

## Usage

Basic usage with Markdown output:
```bash
python pysummary.py dQw4w9WgXcQ
```

Generate a PowerPoint with custom naming:
```bash
python pysummary.py -ppt -name "Rick Astley - Never Gonna Give You Up" dQw4w9WgXcQ
```

Advanced usage (PDF + PPT + Skip N thumbnails):
```bash
python pysummary.py -pdf -ppt -n 2 dQw4w9WgXcQ
```

### Arguments
-   `video_id_or_url`: YouTube Video ID or URL.
-   `-pdf`: Generate PDF output.
-   `-ppt`: Generate PowerPoint output.
-   `-name "FILENAME"`: Specify a custom filename (default uses video ID).
-   `-n X`: Skip first X thumbnails (useful for intro/black screens).
-   `-v`: Verbose output (show FFmpeg logs).
    pip install youtube-transcript-api google-genai python-dotenv requests yt-dlp weasyprint markdown-it-py
    ```
4.  Configure your API key:
    Create a `.env` file in the root directory and add your key:
    ```bash
    GEMINI_API_KEY=your_api_key_here
    ```

## Usage

Provide a YouTube link or Video ID as a command-line argument:

### Using a URL
```bash
python pysummary.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

### Using a Video ID
```bash
python pysummary.py dQw4w9WgXcQ
```

### Generating a PowerPoint Presentation
```bash
python pysummary.py -ppt dQw4w9WgXcQ
```

### Full Multi-format Export with Custom Name
```bash
python pysummary.py dQw4w9WgXcQ -pdf -name "Never Gonna Give You Up" -ppt
```

### 🎬 Example Output (dQw4w9WgXcQ)

#### Video Summary
![Header](https://img.youtube.com/vi/dQw4w9WgXcQ/maxresdefault.jpg)

**Gemma v4 Summary:**
> This transcript consists of the lyrics to Rick Astley's "Never Gonna Give You Up," a song about unwavering loyalty and commitment to a romantic partner.

#### Transcript Preview with Timestamps
![[00:01]](thumbs_dQw4w9WgXcQ/thumb_1.jpg)
**[00:01]** [♪♪♪] ([link](https://youtu.be/dQw4w9WgXcQ?t=1))

![[01:04]](thumbs_dQw4w9WgXcQ/thumb_64.jpg)
**[01:04]** ♪ Your heart's been aching but you're too shy to say it ♪ ([link](https://youtu.be/dQw4w9WgXcQ?t=64))

## Output
The script generates two main outputs:
1.  **Terminal**: A live preview of the transcript and the AI-generated summary.
2.  **Markdown File**: A file named `transcript_<VIDEO_ID>.md` containing the header thumbnail, summary, and the full transcript with embedded images and links.
3.  **Thumbnails Directory**: A `thumbs_<VIDEO_ID>/` folder containing localized `.jpg` images for the transcript line references.

## License
MIT
