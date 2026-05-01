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
from pptx import Presentation
from pptx.util import Inches

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

def generate_summary(text, segment=False):
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
        if segment:
            prompt = f"Please provide a very brief, one-sentence summary of this specific video segment transcript:\n\n{text}"
        else:
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
    Optimized to use faster seek and reduce connection time.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    ydl_opts = {
        'format': 'bestvideo[height<=480]', # Lower resolution for faster extraction
        'quiet': True,
        'no_warnings': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"  [FFmpeg] Getting stream URL for {video_id}...")
            info = ydl.extract_info(url, download=False)
            stream_url = info['url']
            
            # Placing -ss BEFORE -i is much faster as it performs a fast seek at the stream level
            # rather than decoding everything up to that point.
            cmd = [
                'ffmpeg', 
                '-ss', str(timestamp), 
                '-i', stream_url, 
                '-vframes', '1', 
                '-q:v', '5', # Slightly lower quality (5 vs 2) for faster encoding
                output_path, 
                '-y',
                '-loglevel', 'error'
            ]
            
            print(f"  [FFmpeg] Extracting frame at {timestamp}s...")
            start_time = time.time()
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            duration = time.time() - start_time
            print(f"  [FFmpeg] Done in {duration:.2f}s")
            
            stats["ffmpeg_total_time"] += duration
            stats["ffmpeg_calls"] += 1
            
            return os.path.exists(output_path)
    except Exception as e:
        print(f"Frame extraction failed for {timestamp}s: {e}")
        return False

def main():
    # Parse arguments
    generate_pdf = "-pdf" in sys.argv
    generate_ppt = "-ppt" in sys.argv
    no_thumbnails = "-n" in sys.argv
    
    custom_name = None
    if "-name" in sys.argv:
        try:
            name_idx = sys.argv.index("-name")
            custom_name = sys.argv[name_idx + 1]
        except (ValueError, IndexError):
            pass

    # Filter out known flags and their values
    args_to_remove = ["-pdf", "-ppt", "-n"]
    if "-name" in sys.argv:
        args_to_remove.append("-name")
        if custom_name:
            args_to_remove.append(custom_name)
            
    args = [arg for arg in sys.argv[1:] if arg not in args_to_remove]

    if not args:
        print("Usage: python pysummary.py [-pdf] [-ppt] [-n] [-name <filename>] <YouTube URL or Video ID>")
        print("Options:")
        print("  -pdf             Generate a PDF document")
        print("  -ppt             Generate a PowerPoint presentation")
        print("  -n               Skip thumbnail extraction (faster execution)")
        print("  -name <string>   Custom name for output files (instead of Video ID)")
        sys.exit(1)

    input_arg = args[0]
    vid_id = get_video_id(input_arg)

    if not vid_id:
        print(f"Error: Could not extract video ID from '{input_arg}'")
        sys.exit(1)

    # Use custom name or video ID for directory and files
    base_name = custom_name if custom_name else vid_id
    thumbs_dir = f"thumbs_{base_name}"
    os.makedirs(thumbs_dir, exist_ok=True)

    try:
        # In newer versions, YouTubeTranscriptApi is an object that needs instantiation
        print(f"Fetching transcript for {vid_id}...")
        api = yta()
        transcript = api.fetch(vid_id)
        
        # In version 1.2.4, FetchedTranscript has a 'snippets' attribute
        # which is a list of FetchedTranscriptSnippet (which are essentially dicts)
        # or we can use to_raw_data()
        data = transcript.to_raw_data()
        print(f"Transcript fetched. Found {len(data)} segments.")
        
        # Add a header image to the .md file (Main Thumbnail)
        main_thumbnail = f"![Thumbnail](https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg)"
        
        # Format transcript with timestamps
        formatted_lines = []
        # Store segments for PPT generation
        ppt_slides_data = []
        
        # Limiting thumbnails to avoid too many downloads for long videos
        # We can take a thumbnail every 60 seconds or so
        last_thumb_time = -70 

        print("Processing transcript segments...")
        for i, segment in enumerate(data):
            start_time = segment['start']
            duration = segment.get('duration', 0)
            end_time = start_time + duration
            # Convert decimal seconds to MM:SS or HH:MM:SS format
            minutes, seconds = divmod(int(start_time), 60)
            hours, minutes = divmod(minutes, 60)
            
            if hours > 0:
                timestamp_str = f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"
            else:
                timestamp_str = f"[{minutes:02d}:{seconds:02d}]"
                
            jump_url = f"https://youtu.be/{vid_id}?t={int(start_time)}"
            text = segment['text'].replace('\n', ' ').strip()
            
            # Simple heuristic to include a thumbnail every ~60 seconds
            if start_time - last_thumb_time >= 60:
                current_thumb_path = None
                if not no_thumbnails:
                    print(f"  [{i}/{len(data)}] Processing thumbnail at {timestamp_str}...")
                    thumb_name = f"thumb_{int(start_time)}.jpg"
                    current_thumb_path = os.path.join(thumbs_dir, thumb_name)
                    
                    if not os.path.exists(current_thumb_path):
                        # Try to extract the real frame from the video stream
                        success = extract_frame(vid_id, int(start_time), current_thumb_path)
                        
                        # Fallback to HQ thumbnail if extraction fails
                        if not success:
                            print(f"  [Fallback] Downloading HQ thumbnail for {timestamp_str}")
                            r = requests.get(f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg")
                            if r.status_code == 200:
                                with open(current_thumb_path, 'wb') as f:
                                    f.write(r.content)
                    
                    formatted_lines.append(f"![{timestamp_str}]({current_thumb_path})\n")
                else:
                    # Just add timestamp if thumbnails are disabled
                    formatted_lines.append(f"**{timestamp_str}**\n")
                
                # Start a new PPT slide entry
                ppt_slides_data.append({
                    "start_timestamp": timestamp_str,
                    "end_timestamp": timestamp_str, # Will be updated by following segments
                    "start_time": start_time,
                    "end_time": end_time,
                    "image": current_thumb_path,
                    "text": text
                })
                last_thumb_time = start_time
            else:
                # Append text to the current PPT slide notes and update end timestamp
                if ppt_slides_data:
                    ppt_slides_data[-1]["text"] += " " + text
                    ppt_slides_data[-1]["end_time"] = end_time
                    
                    # Update the end timestamp for the range
                    end_minutes, end_seconds = divmod(int(end_time), 60)
                    end_hours, end_minutes = divmod(end_minutes, 60)
                    if end_hours > 0:
                        ppt_slides_data[-1]["end_timestamp"] = f"[{end_hours:02d}:{end_minutes:02d}:{end_seconds:02d}]"
                    else:
                        ppt_slides_data[-1]["end_timestamp"] = f"[{end_minutes:02d}:{end_seconds:02d}]"
            
            formatted_lines.append(f"{timestamp_str} {text} ([link]({jump_url}))")

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
        filename_md = f"transcript_{base_name}.md"
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
            filename_pdf = f"transcript_{base_name}.pdf"
            
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
            
            # Use the absolute path to the directory containing images as base_url
            HTML(string=styled_html, base_url=os.path.join(cwd, thumbs_dir)).write_pdf(filename_pdf)
            print(f"Success: PDF saved to {filename_pdf}")

        # PPT Generation
        if generate_ppt:
            print("\n--- Generating PowerPoint ---")
            filename_ppt = f"transcript_{base_name}.pptx"
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
            title.text = f"Video Summary: {vid_id}"
            
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
            tf.text = summary if len(summary) < 1000 else summary[:1000] + "..."
            tf.word_wrap = True

            # --- Transcript Slides ---
            from pptx.enum.text import PP_ALIGN
            from pptx.dml.color import RGBColor
            from pptx.util import Pt

            for slide_data in ppt_slides_data:
                # Use a blank layout for maximum image space
                blank_slide_layout = prs.slide_layouts[6]
                slide = prs.slides.add_slide(blank_slide_layout)
                
                # Add image if available
                if slide_data["image"] and os.path.exists(slide_data["image"]):
                    # Center the image on the slide
                    left = Inches(1)
                    top = Inches(0.5)
                    width = Inches(8) # Standard slide width is 10 inches
                    slide.shapes.add_picture(slide_data["image"], left, top, width=width)
                    
                    # Add timestamp range below the thumbnail
                    # 7.5 inches down is roughly below an 8-inch width image starting at 0.5 top
                    txBox = slide.shapes.add_textbox(left, Inches(6.5), width, Inches(1))
                    tf = txBox.text_frame
                    p = tf.paragraphs[0]
                    p.alignment = PP_ALIGN.CENTER

                    # Add "Range: " prefix
                    run = p.add_run()
                    run.text = "Range: "
                    run.font.size = Pt(18)
                    run.font.color.rgb = RGBColor(0, 0, 0)

                    # Add Start Timestamp with Link
                    start_run = p.add_run()
                    start_run.text = slide_data['start_timestamp']
                    start_run.font.size = Pt(18)
                    start_run.font.color.rgb = RGBColor(5, 99, 193) # Standard blue link color
                    start_run.font.underline = True
                    start_time_seconds = int(slide_data['start_time'])
                    start_run.hyperlink.address = f"https://youtu.be/{vid_id}?t={start_time_seconds}"

                    # Add separator
                    sep_run = p.add_run()
                    sep_run.text = " - "
                    sep_run.font.size = Pt(18)
                    sep_run.font.color.rgb = RGBColor(0, 0, 0)

                    # Add End Timestamp with Link
                    end_run = p.add_run()
                    end_run.text = slide_data['end_timestamp']
                    end_run.font.size = Pt(18)
                    end_run.font.color.rgb = RGBColor(5, 99, 193)
                    end_run.font.underline = True
                    end_time_seconds = int(slide_data['end_time'])
                    end_run.hyperlink.address = f"https://youtu.be/{vid_id}?t={end_time_seconds}"
                
                # Add timestamp and text to speaker notes
                notes_slide = slide.notes_slide
                text_frame = notes_slide.notes_text_frame
                
                print(f"  Generating summary for slide {slide_data['start_timestamp']}...")
                segment_summary = generate_summary(slide_data['text'], segment=True)
                
                text_frame.text = f"Range: {slide_data['start_timestamp']} - {slide_data['end_timestamp']}\n\n" \
                                f"Segment Summary: {segment_summary}\n\n" \
                                f"Transcript: {slide_data['text']}"
            
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
