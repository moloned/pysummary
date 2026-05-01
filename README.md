# PySummary

A sophisticated YouTube transcript downloader and summarizer that generates rich, timestamped Markdown reports with AI-driven summaries and visual thumbnails.

## Features
-   **Multi-format Input**: Extract transcripts using full YouTube URLs, shortened `youtu.be` links, or direct Video IDs.
-   **AI Summarization**: Automatically generates concise summaries using **Gemma v4** (via Google Gemini API).
-   **Rich Markdown & PDF Output**: Creates detailed `.md` and `.pdf` files for every video with embedded imagery.
-   **Visual Timestamps**: 
    -   Includes clickable links to jump to specific moments on YouTube.
    -   Extracts and embeds unique timestamped frames (every ~60 seconds) using `ffmpeg` and `yt-dlp` for precise visual context.
-   **Execution Statistics**: Tracks and reports FFmpeg processing time and AI token usage.
-   **Clean Formatting**: Removes unnecessary newlines and whitespace for a polished reading experience.

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

### Generating a PDF Report
```bash
python pysummary.py -pdf dQw4w9WgXcQ
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
