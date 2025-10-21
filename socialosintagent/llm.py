import base64
import collections
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from openai import (APIError, AuthenticationError, BadRequestError, OpenAI,
                    RateLimitError)
from openai.types.chat import ChatCompletion
from PIL import Image

from .exceptions import RateLimitExceededError
from .utils import (SUPPORTED_IMAGE_EXTENSIONS, extract_and_resolve_urls,
                    get_sort_key)

logger = logging.getLogger("SocialOSINTAgent.llm")


class LLMAnalyzer:
    _llm_completion_object: Optional[ChatCompletion] = None
    _llm_api_exception: Optional[Exception] = None

    def __init__(self, is_offline: bool):
        self.is_offline = is_offline
        self._llm_client_instance: Optional[OpenAI] = None

    @property
    def client(self) -> OpenAI:
        """Initializes and returns the OpenAI client for LLM calls."""
        if self._llm_client_instance is None:
            try:
                api_key = os.environ["LLM_API_KEY"]
                base_url = os.environ["LLM_API_BASE_URL"]
                
                headers: Dict[str, str] = {}
                if "openrouter.ai" in base_url.lower():
                    headers["HTTP-Referer"] = os.getenv("OPENROUTER_REFERER", "http://localhost:3000")
                    headers["X-Title"] = os.getenv("OPENROUTER_X_TITLE", "SocialOSINTAgent")

                self._llm_client_instance = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=httpx.Timeout(60.0, connect=10.0),
                    default_headers=headers or None,
                )
                logger.info(f"LLM client initialized for base URL: {base_url}")
            except KeyError as e:
                raise RuntimeError(f"LLM config missing: {e} not found in environment.")
            except Exception as e:
                raise RuntimeError(f"Failed to initialize LLM client: {e}")
        return self._llm_client_instance

    def _call_llm_api(self, model_name: str, messages: list, max_tokens: int, temperature: float):
        """Helper method to make the LLM API call, designed to be run in a thread."""
        self._llm_completion_object = None
        self._llm_api_exception = None
        try:
            self._llm_completion_object = self.client.chat.completions.create(
                model=model_name, messages=messages, max_tokens=max_tokens, temperature=temperature
            )
        except APIError as e:
            self._llm_api_exception = e
        except Exception as e:
            self._llm_api_exception = e

    def analyze_image(self, file_path: Path, source_url: str, context: str = "") -> Optional[str]:
        if self.is_offline:
            logger.info(f"Offline mode: Skipping LLM image analysis for {file_path}.")
            return None
        if not file_path.exists() or file_path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            return None

        temp_path = None
        try:
            with Image.open(file_path) as img:
                img_to_process = img
                if getattr(img, "is_animated", False):
                    img.seek(0)
                    img_to_process = img.copy()
                if img_to_process.mode != "RGB":
                    if img_to_process.mode == "P" and "transparency" in img_to_process.info:
                        img_to_process = img_to_process.convert("RGBA")
                    if img_to_process.mode == "RGBA":
                        bg = Image.new("RGB", img_to_process.size, (255, 255, 255))
                        bg.paste(img_to_process, mask=img_to_process.split()[3])
                        img_to_process = bg
                    else:
                        img_to_process = img_to_process.convert("RGB")
                
                max_dim = 1536
                if max(img_to_process.size) > max_dim:
                    img_to_process.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                
                temp_path = file_path.with_suffix(".processed.jpg")
                img_to_process.save(temp_path, "JPEG", quality=85)
                analysis_file_path = temp_path

            base64_image = base64.b64encode(analysis_file_path.read_bytes()).decode("utf-8")
            image_data_url = f"data:image/jpeg;base64,{base64_image}"

            prompt_text = (
                f"Perform an objective OSINT analysis of this image originating from {context}. Focus *only* on visually verifiable elements relevant to profiling or context understanding. Describe:\n"
                "- **Setting/Environment:** (e.g., Indoor office, outdoor urban street, natural landscape, specific room type if identifiable). Note weather, time of day clues, architecture if distinctive.\n"
                "- **Key Objects/Items:** List prominent or unusual objects. If text/logos are clearly legible (e.g., book titles, brand names on products, signs), state them exactly. Note technology types, tools, personal items.\n"
                "- **People (if present):** Describe observable characteristics: approximate number, general attire, estimated age range (e.g., child, adult, senior), ongoing activity. *Do not guess identities or relationships.*\n"
                "- **Text/Symbols:** Transcribe any clearly readable text on signs, labels, clothing, etc. Describe distinct symbols or logos.\n"
                "- **Activity/Event:** Describe the apparent action (e.g., person working at desk, group dining, attending rally, specific sport).\n"
                "- **Implicit Context Indicators:** Note subtle clues like reflections revealing unseen elements, background details suggesting location (e.g., specific landmarks, regional flora), or object condition suggesting usage/age.\n"
                "- **Overall Scene Impression:** Summarize the visual narrative (e.g., professional setting, casual gathering, technical workshop, artistic expression, political statement).\n\n"
                "Output a concise, bulleted list of observations. Avoid assumptions, interpretations, or emotional language not directly supported by the visual evidence."
            )
            model = os.environ["IMAGE_ANALYSIS_MODEL"]
            completion = self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt_text}, {"type": "image_url", "image_url": {"url": image_data_url, "detail": "high"}}]}],
                max_tokens=1024,
            )
            analysis_text = completion.choices[0].message.content.strip() if completion.choices[0].message.content else None
            if not analysis_text:
                return None
            
            # **CHANGE**: Format the output to be more structured and explicit.
            return f"- **Image Source:** [{source_url}]({source_url})\n- **Analysis:**\n{analysis_text}"

        except APIError as e:
            if isinstance(e, RateLimitError): raise RateLimitExceededError("LLM Image Analysis")
            logger.error(f"LLM API error during image analysis: {e}")
            return None
        except Exception as e:
            logger.error(f"Error during image analysis for {file_path}: {e}", exc_info=True)
            return None
        finally:
            if temp_path and temp_path.exists(): temp_path.unlink()

    def _format_text_data(self, platform: str, username: str, data: dict) -> str:
        """Formats fetched data into a detailed text summary for the LLM."""

        MAX_ITEMS_PER_TYPE = 25
        TEXT_SNIPPET_LENGTH = 750
        if not data: return ""
        
        output = []
        user_info = data.get("user_info") or data.get("profile_info") or data.get("user_profile")
        prefix = {"twitter": "@", "reddit": "u/"}.get(platform, "")
        handle = user_info.get("username") or user_info.get("name") or user_info.get("handle") or user_info.get("acct") or username if user_info else username
        output.append(f"### {platform.capitalize()} Data Summary for: {prefix}{handle}")

        if user_info:
            output.append("\n**User Profile:**")
            created = get_sort_key(user_info, "created_at") or get_sort_key(user_info, "created_utc")
            output.append(f"- Account Created: {created.strftime('%Y-%m-%d') if created > datetime.min.replace(tzinfo=timezone.utc) else 'N/A'}")
            if platform == "twitter":
                pm = user_info.get("public_metrics", {})
                output.append(f"- Description: {user_info.get('description', '')}")
                output.append(f"- Stats: Followers={pm.get('followers_count','N/A')}, Following={pm.get('following_count','N/A')}, Tweets={pm.get('tweet_count','N/A')}")
            elif platform == "reddit":
                output.append(f"- Karma: Link={user_info.get('link_karma','N/A')}, Comment={user_info.get('comment_karma','N/A')}")
            elif platform == "mastodon":
                output.append(f"- Bio: {user_info.get('note_text', '')}")
                output.append(f"- Stats: Followers={user_info.get('followers_count','N/A')}, Following={user_info.get('following_count','N/A')}, Posts={user_info.get('statuses_count','N/A')}")

        if data.get("stats"):
            output.append("\n**Cached Activity Overview:**")
            output.append(f"- {json.dumps(data['stats'])}")
        
        # Detailed Item Formatting with null checks
        if platform == "twitter" and data.get("tweets"):
            output.append(f"\n**Recent Tweets (up to {MAX_ITEMS_PER_TYPE}):**")
            for i, t in enumerate(data["tweets"][:MAX_ITEMS_PER_TYPE]):
                if t is not None:  # Null check for tweets
                    ts = get_sort_key(t, "created_at").strftime("%Y-%m-%d")
                    info = []
                    if t.get("replied_to_user_info"): info.append(f"Reply to @{t['replied_to_user_info']['username']}")
                    if any(r['type'] == 'quoted' for r in t.get("referenced_tweets",[])): info.append("Quotes a tweet")
                    if t.get("media"): info.append(f"Media: {len(t['media'])}")
                    info_str = f" ({', '.join(info)})" if info else ""
                    text = t.get("text", "")[:TEXT_SNIPPET_LENGTH]
                    output.append(f"- Tweet {i+1} ({ts}){info_str}:\n  Content: {text}\n  Metrics: {t.get('metrics')}")
        elif platform == "reddit":
            if data.get("submissions"):
                output.append(f"\n**Recent Submissions (up to {MAX_ITEMS_PER_TYPE}):**")
                for i, s in enumerate(data["submissions"][:MAX_ITEMS_PER_TYPE]):
                    if s is not None:  # Null check for submissions
                        ts = get_sort_key(s, "created_utc").strftime("%Y-%m-%d")
                        output.append(f"- Submission {i+1} in r/{s.get('subreddit','?')} ({ts}):\n  Title: {s.get('title')}\n  Score: {s.get('score',0)}")
            if data.get("comments"):
                output.append(f"\n**Recent Comments (up to {MAX_ITEMS_PER_TYPE}):**")
                for i, c in enumerate(data["comments"][:MAX_ITEMS_PER_TYPE]):
                    if c is not None:  # Null check for comments
                        ts = get_sort_key(c, "created_utc").strftime("%Y-%m-%d")
                        text = c.get("text","")[:TEXT_SNIPPET_LENGTH]
                        output.append(f"- Comment {i+1} in r/{c.get('subreddit','?')} ({ts}):\n  Content: {text}\n  Score: {c.get('score',0)}")
        elif platform == "mastodon" and data.get("posts"):
            output.append(f"\n**Recent Posts (up to {MAX_ITEMS_PER_TYPE}):**")
            for i, p in enumerate(data["posts"][:MAX_ITEMS_PER_TYPE]):
                if p is not None:  # Null check for posts
                    ts = get_sort_key(p, "created_at").strftime("%Y-%m-%d")
                    info = ["Boost"] if p.get("is_reblog") else []
                    if p.get("media"): info.append(f"Media: {len(p['media'])}")
                    info_str = f" ({', '.join(info)})" if info else ""
                    text = p.get("text_cleaned", "")[:TEXT_SNIPPET_LENGTH]
                    output.append(f"- Post {i+1} ({ts}){info_str}:\n  Content: {text}\n  Stats: Favs={p.get('favourites_count',0)}, Boosts={p.get('reblogs_count',0)}")

        return "\n".join(output)

    def _analyze_shared_links(self, platforms_data: Dict[str, List[Dict]]) -> str:
        """Extracts all external links, counts domains, and returns a markdown summary."""
        all_urls = []
        platform_domains = {
            "twitter.com", "x.com", "t.co", "reddit.com", "redd.it", "bsky.app",
            "news.ycombinator.com", "youtube.com", "youtu.be" # Exclude youtube as it's too common
        }

        try:
            for platform, user_data_list in platforms_data.items():
                if not isinstance(user_data_list, list):
                    continue
                    
                for user_data in user_data_list:
                    if not isinstance(user_data, dict):
                        continue
                        
                    data = user_data.get("data", {})
                    if not isinstance(data, dict):
                        continue
                    
                    if platform == "twitter":
                        tweets = data.get("tweets", [])
                        if not isinstance(tweets, list):
                            continue
                        
                        for t in tweets:
                            # Multiple levels of safety checks
                            if t is None or not isinstance(t, dict):
                                continue
                            
                            entities_raw = t.get("entities_raw")
                            if entities_raw is None or not isinstance(entities_raw, dict):
                                continue
                                
                            urls = entities_raw.get("urls")
                            if urls is None or not isinstance(urls, list):
                                continue
                                
                            for url_entity in urls:
                                if isinstance(url_entity, dict) and "expanded_url" in url_entity:
                                    all_urls.append(url_entity["expanded_url"])
                                    
                    elif platform == "reddit":
                        submissions = data.get("submissions", [])
                        if isinstance(submissions, list):
                            for s in submissions:
                                if s is not None and isinstance(s, dict):
                                    if s.get("link_url"): 
                                        all_urls.append(s["link_url"])
                                    if s.get("text"): 
                                        all_urls.extend(extract_and_resolve_urls(s["text"]))
                        
                        comments = data.get("comments", [])
                        if isinstance(comments, list):
                            for c in comments:
                                if c is not None and isinstance(c, dict):
                                    if c.get("text"): 
                                        all_urls.extend(extract_and_resolve_urls(c["text"]))
                                        
                    elif platform == "hackernews":
                        items = data.get("items", [])
                        if isinstance(items, list):
                            for i in items:
                                if i is not None and isinstance(i, dict):
                                    if i.get("url"): 
                                        all_urls.append(i["url"])
                                    if i.get("text"): 
                                        all_urls.extend(extract_and_resolve_urls(i["text"]))
                                        
                    else: # Generic text extraction for Bluesky, Mastodon
                        posts = data.get("posts", [])
                        if isinstance(posts, list):
                            for p in posts:
                                if p is not None and isinstance(p, dict):
                                    if p.get("text"): 
                                        all_urls.extend(extract_and_resolve_urls(p["text"]))
                                    if p.get("text_cleaned"): 
                                        all_urls.extend(extract_and_resolve_urls(p["text_cleaned"]))

        except Exception as e:
            logger.error(f"Error in _analyze_shared_links: {e}", exc_info=True)
            return ""  # Return empty string instead of crashing

        if not all_urls:
            return ""

        domain_counts = collections.Counter()
        for url in all_urls:
            try:
                domain = urlparse(url).netloc.replace("www.", "")
                if domain and domain not in platform_domains:
                    domain_counts[domain] += 1
            except Exception:
                continue

        if not domain_counts:
            return ""

        output = ["## Top Shared Domains"]
        for domain, count in domain_counts.most_common(10):
            output.append(f"- **{domain}:** {count} link(s)")
        
        return "\n".join(output)

    def run_analysis(self, platforms_data: Dict[str, List[Dict]], query: str) -> str:
        """Collects data summaries and uses LLM to analyze it."""
        collected_summaries, all_media_analyses = [], []
        
        current_ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        for platform, user_data_list in platforms_data.items():
            for user_data in user_data_list:
                username = user_data.get("username_key", "unknown")
                summary = self._format_text_data(platform, username, user_data["data"])
                if summary: collected_summaries.append(summary)
                media_analyses = [ma for ma in user_data["data"].get("media_analysis", []) if ma]
                if media_analyses: all_media_analyses.extend(media_analyses)
        
        if not collected_summaries and not all_media_analyses:
            return "[yellow]No data available for analysis.[/yellow]"

        components = []
        if all_media_analyses:
            components.append("## Consolidated Media Analysis:\n\n" + "\n\n".join(sorted(list(set(all_media_analyses)))))
        
        shared_links_summary = self._analyze_shared_links(platforms_data)
        if shared_links_summary:
            components.append(shared_links_summary)

        if collected_summaries:
            components.append("## Collected Textual & Activity Data Summary:\n\n" + "\n\n---\n\n".join(collected_summaries))
        
        system_prompt = f"""**Objective:** Generate a comprehensive behavioral and linguistic profile based on the provided social media data, employing structured analytic techniques focused on objectivity, evidence-based reasoning, and clear articulation.

**IMPORTANT CONTEXT: The current date and time for this analysis is {current_ts_str}.** All dates in the provided data should be interpreted relative to this timestamp.

**Input:** You will receive summaries of user activity, itemized/linked descriptive analyses of images, and a summary of top shared external domains. The user will provide a specific analysis query.

**Primary Task:** Address the user's specific analysis query using ALL the data provided (text, image analyses, and shared domains) and the analytical framework below.

**Analysis Domains (Use these to structure your thinking and response where relevant to the query):**
1.  **Behavioral Patterns:** Analyze interaction frequency, platform-specific activity, and temporal communication rhythms.
2.  **Semantic Content & Themes:** Identify recurring topics, keywords, and concepts. Analyze linguistic indicators like sentiment/tone and cognitive framing.
3.  **Interests & Network Context:** Deduce primary interests, hobbies, or professional domains. Use the **Shared Domain Analysis** to identify primary information sources. Note interaction patterns visible *within the provided posts*.
4.  **Communication Style:** Assess linguistic complexity, use of jargon/slang, and markers of emotional expression.
5.  **Network Connections:** Systematically list all mentioned, replied-to, or boosted/retweeted accounts found in the data, noting the nature of the interaction.
6.  **Visual Data Integration:** Explicitly incorporate insights derived from the provided image analyses.

**Analytical Constraints & Guidelines:**
*   **Temporal Awareness:** Use the provided current date to correctly interpret the timeline of events. Do not rely on your internal knowledge cutoff date.
*   **Image Link Preservation:** When citing evidence from a specific image, you MUST include the original clickable Markdown link to the `Image Source` exactly as provided. This is critical for report verifiability. DO NOT remove the link or reference the image only by its filename.
*   **Evidence-Based:** Ground ALL conclusions *strictly and exclusively* on the provided source materials (text summaries, image analyses, and the shared domain list).
*   **Objectivity & Neutrality:** Maintain analytical neutrality. Avoid speculation, moral judgments, or personal opinions not present in the data.
*   **Synthesize, Don't Just List:** Integrate findings from different platforms and data types into a coherent narrative that addresses the query.
*   **Address the Query Directly:** Structure your response primarily around answering the user's specific question(s).
*   **Acknowledge Limitations:** If the data is sparse, lacks specific details, or if certain data types were unavailable/unprocessed, explicitly state these limitations.

**Output:** A structured analytical report that directly addresses the user's query, rigorously supported by evidence from the provided text, image, and link data, adhering to all constraints.
"""
        
        user_prompt = f"**Analysis Query:** {query}\n\n**Provided Data:**\n\n" + "\n\n===\n\n".join(components)
        
        text_model = os.environ["ANALYSIS_MODEL"]
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        api_thread = threading.Thread(target=self._call_llm_api, kwargs={"model_name": text_model, "messages": messages, "max_tokens": 3500, "temperature": 0.5})
        api_thread.start()
        api_thread.join()

        if self._llm_api_exception:
            raise RuntimeError(f"LLM API request failed") from self._llm_api_exception
        if not self._llm_completion_object or not self._llm_completion_object.choices:
            raise RuntimeError("LLM API call returned no completion.")
        
        return self._llm_completion_object.choices[0].message.content or ""