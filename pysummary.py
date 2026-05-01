import sys
import re
import os
import requests
import yt_dlp
import subprocess
import time
from dotenv import load_dotenv
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi as yta
from markdown_it import MarkdownIt
from weasyprint import HTML

# Load environment variables (for API key)
load_dotenv()

# Global statistics
stats = {
    "ffmpeg_total_time": 0.0,
    "ffmpeg_calls": 0,
    "tokens_prompt": 0,
    "tokens_response": 0
}

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

def generate_summary(text):
    """
    Generates a summary of the transcript using Gemma (via Google Generative AI).
    Supports either an API key or a local setup if configured.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    
    if not api_key:
        return "Summary not generated: GEMINI_API_KEY environment variable not found."

    try:
        client = genai.Client(api_key=api_key)
        # Using the specific Gemma model requested: gemma-4-31b-it
        prompt = f"Please provide a concise summary of the following YouTube transcript:\n\n{text}"
        response = client.models.generate_content(
            model='models/gemma-4-31b-it',
            contents=prompt
        )
        
        # Track tokens if metadata is available
        if hasattr(response, 'usage_metadata'):
            stats["tokens_prompt"] += response.usage_metadata.prompt_token_count
            stats["tokens_response"] += response.usage_metadata.candidates_token_count
            
        return response.text
    except Exception as e:
        return f"Error generating summary: {e}"

def extract_frame(video_id, timestamp, output_path):
    """
    Extracts a single frame from the YouTube video stream at the specified timestamp.
    Requires yt-dlp. ffmpeg or other tools may be needed by yt-dlp.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # We use yt-dlp to get the URL of the best image/video stream and then use it
    # However, a simpler way is to use yt-dlp's frame extraction if available
    # Or just use the --get-url and pass to a tool.
    # Given the environment, we'll try yt-dlp with --skip-download and --write-thumbnail 
    # but that's for the main thumbnail. 
    # For a specific timestamp, we can use the following command structure:
    # yt-dlp -g -f bestvideo [URL] 
    # then ffmpeg -ss [time] -i [url] -vframes 1 [output]
    
    ydl_opts = {
        'format': 'bestvideo',
        'quiet': True,
        'no_warnings': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info['url']
            
            # Use subprocess to call ffmpeg if available, 
            # or try yt-dlp's internal extraction if possible.
            # Since ffmpeg might not be directly in PATH but might be available to yt-dlp,
            # we'll try a common ffmpeg command via subprocess.
            # If ffmpeg is missing (checked earlier and failed), we might need an alternative.
            # But let's try calling it anyway to be sure, or use a fallback.
            
            cmd = [
                'ffmpeg', 
                '-ss', str(timestamp), 
                '-i', stream_url, 
                '-vframes', '1', 
                '-q:v', '2', 
                output_path, 
                '-y'
            ]
            
            start_time = time.time()
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            duration = time.time() - start_time
            
            stats["ffmpeg_total_time"] += duration
            stats["ffmpeg_calls"] += 1
            
            return os.path.exists(output_path)
    except Exception as e:
        print(f"Frame extraction failed for {timestamp}s: {e}")
        return False

def main():
    # Parse arguments
    generate_pdf = "-pdf" in sys.argv
    args = [arg for arg in sys.argv[1:] if arg != "-pdf"]

    if not args:
        print("Usage: python pysummary.py [-pdf] <YouTube URL or Video ID>")
        sys.exit(1)

    input_arg = args[0]
    vid_id = get_video_id(input_arg)

    if not vid_id:
        print(f"Error: Could not extract video ID from '{input_arg}'")
        sys.exit(1)

    # Create thumbnails directory
    thumbs_dir = f"thumbs_{vid_id}"
    os.makedirs(thumbs_dir, exist_ok=True)

    try:
        # In newer versions, YouTubeTranscriptApi is an object that needs instantiation
        api = yta()
        transcript = api.fetch(vid_id)
        
        # In version 1.2.4, FetchedTranscript has a 'snippets' attribute
        # which is a list of FetchedTranscriptSnippet (which are essentially dicts)
        # or we can use to_raw_data()
        data = transcript.to_raw_data()
        
        # Add a header image to the .md file (Main Thumbnail)
        main_thumbnail = f"![Thumbnail](https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg)"
        
        # Format transcript with timestamps
        formatted_lines = []
        # Limiting thumbnails to avoid too many downloads for long videos
        # We can take a thumbnail every 30 seconds or so
        last_thumb_time = -40 

        for segment in data:
            start_time = segment['start']
            # Convert decimal seconds to MM:SS or HH:MM:SS format
            minutes, seconds = divmod(int(start_time), 60)
            hours, minutes = divmod(minutes, 60)
            
            if hours > 0:
                timestamp = f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"
            else:
                timestamp = f"[{minutes:02d}:{seconds:02d}]"
                
            jump_url = f"https://youtu.be/{vid_id}?t={int(start_time)}"
            text = segment['text'].replace('\n', ' ').strip()
            
            line = f"{timestamp} {text} ([link]({jump_url}))"
            
            # Simple heuristic to include a thumbnail every ~30 seconds
            if start_time - last_thumb_time >= 30:
                # Note: YouTube doesn't allow random frame extraction via simple URL.
                # However, many people use this trick with a specific pattern if available,
                # but standardly only hqdefault/mqdefault/maxresdefault exist.
                # For per-timestamp thumbnails, typically one needs to download the video 
                # or use storyboard URLs. storyboard URLs are complex.
                # Instead, we'll download the MQ thumbnail for the video as a placeholder 
                # or acknowledge the limitation if it's not possible without complex logic.
                #
                # ACTUALLY, I will download the HQ thumbnail as a representative image 
                # for the section if I can find a way, but since YouTube only provides 
                # static thumbnails, I'll just save the main one in the folder 
                # and link it periodically in the MD.
                
                thumb_name = f"thumb_{int(start_time)}.jpg"
                thumb_path = os.path.join(thumbs_dir, thumb_name)
                
                if not os.path.exists(thumb_path):
                    # Try to extract the real frame from the video stream
                    success = extract_frame(vid_id, int(start_time), thumb_path)
                    
                    # Fallback to HQ thumbnail if extraction fails
                    if not success:
                        r = requests.get(f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg")
                        if r.status_code == 200:
                            with open(thumb_path, 'wb') as f:
                                f.write(r.content)
                
                line = f"![{timestamp}]({thumb_path})\n\n{line}"
                last_thumb_time = start_time

            formatted_lines.append(line)

        output_content = '\n'.join(formatted_lines)
        
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
        filename_md = f"transcript_{vid_id}.md"
        with open(filename_md, "w", encoding="utf-8") as f:
            f.write(f"# Transcript and Summary for YouTube Video: {vid_id}\n\n")
            f.write(f"{main_thumbnail}\n\n")
            f.write("## Summary\n")
            f.write(f"{summary}\n\n")
            f.write("## Transcript\n")
            f.write(output_content)
            f.write(stats_block)
        
        print(f"\nSuccess: Transcript and Summary saved to {filename_md}")

        # PDF Generation
        if generate_pdf:
            print("\n--- Generating PDF ---")
            filename_pdf = f"transcript_{vid_id}.pdf"
            
            # Use markdown-it-py to convert MD to HTML
            md = MarkdownIt("commonmark", {
                "html": True,
                "linkify": True,
            })
            
            # We need to make image paths absolute for WeasyPrint
            cwd = os.getcwd()
            md_content = f"""
# Transcript and Summary for YouTube Video: {vid_id}

<img src="https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg" style="width: 100%; max-width: 800px; display: block; margin: 0 auto;">

## Summary
{summary}

## Transcript
{output_content.replace('](' + thumbs_dir + '/', '](' + 'file://' + os.path.join(cwd, thumbs_dir) + '/')}

{stats_block}
"""
            html_content = md.render(md_content)
            
            # Simple CSS for the PDF
            styled_html = f"""
            <html>
                <head>
                    <style>
                        body {{ font-family: sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 40px auto; padding: 0 20px; }}
                        h1 {{ color: #1a73e8; text-align: center; }}
                        h2 {{ color: #444; border-bottom: 1px solid #ddd; padding-bottom: 10px; margin-top: 30px; }}
                        blockquote {{ background: #f9f9f9; border-left: 5px solid #ccc; margin: 1.5em 10px; padding: 0.5em 10px; font-style: italic; }}
                        img {{ max-width: 100%; height: auto; border-radius: 4px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 20px 0; }}
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
            
            HTML(string=styled_html, base_url=cwd).write_pdf(filename_pdf)
            print(f"Success: PDF saved to {filename_pdf}")

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
