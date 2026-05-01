import sys
import re
import os
import requests
import yt_dlp
import subprocess
from dotenv import load_dotenv
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi as yta

# Load environment variables (for API key)
load_dotenv()

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
        genai.configure(api_key=api_key)
        # Using the specific Gemma model requested: gemma-4-31b-it
        model = genai.GenerativeModel('models/gemma-4-31b-it') 
        
        prompt = f"Please provide a concise summary of the following YouTube transcript:\n\n{text}"
        response = model.generate_content(prompt)
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
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return os.path.exists(output_path)
    except Exception as e:
        print(f"Frame extraction failed for {timestamp}s: {e}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python pysummary.py <YouTube URL or Video ID>")
        sys.exit(1)

    input_arg = sys.argv[1]
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
        
        # Output to .md file
        filename = f"transcript_{vid_id}.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# Transcript and Summary for YouTube Video: {vid_id}\n\n")
            f.write(f"{main_thumbnail}\n\n")
            f.write("## Summary\n")
            f.write(f"{summary}\n\n")
            f.write("## Transcript\n")
            f.write(output_content)
        
        print(f"\nSuccess: Transcript and Summary saved to {filename}")

    except Exception as e:
        print(f"Error fetching transcript: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
