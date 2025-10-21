import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import humanize
import praw
import tweepy
from atproto import Client
from atproto import exceptions as atproto_exceptions
from dotenv import load_dotenv
from mastodon import Mastodon
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .cache import CacheManager
from .exceptions import (AccessForbiddenError, RateLimitExceededError,
                         UserNotFoundError)
from .llm import LLMAnalyzer
from .platforms import FETCHERS
from .utils import handle_rate_limit, sanitize_username

# Load environment variables from a .env file in the project root
project_root = Path(__file__).resolve().parent.parent
dotenv_path = project_root / '.env'
load_dotenv(dotenv_path=dotenv_path, override=True)

logger = logging.getLogger("SocialOSINTAgent")

class SocialOSINTAgent:
    def __init__(self, args):
        self.console = Console()
        self.args = args
        self.base_dir = Path("data")
        self._setup_directories()
        self.cache = CacheManager(self.base_dir, self.args.offline)
        self.llm = LLMAnalyzer(self.args.offline)
        self._twitter: Optional[tweepy.Client] = None
        self._reddit: Optional[praw.Reddit] = None
        self._bluesky: Optional[Client] = None
        self._mastodon_clients: Dict[str, Mastodon] = {}
        self._default_mastodon_lookup_client: Optional[Mastodon] = None
        self._mastodon_clients_initialized: bool = False
        self._verify_env_vars()
    
    def _verify_env_vars(self):
        """Verifies that necessary environment variables are set."""
        required_llm = ["LLM_API_KEY", "LLM_API_BASE_URL", "IMAGE_ANALYSIS_MODEL", "ANALYSIS_MODEL"]
        if any(not os.getenv(k) for k in required_llm):
            raise RuntimeError("Missing one or more critical LLM environment variables (LLM_API_KEY, LLM_API_BASE_URL, IMAGE_ANALYSIS_MODEL, ANALYSIS_MODEL).")
        
        platforms_configured = self.get_available_platforms(check_creds=True)
        if not platforms_configured:
             logger.warning("No platform API credentials found or Mastodon config is invalid. Only HackerNews may be available.")

    def _setup_directories(self):
        """Ensures necessary directories exist."""
        for dir_name in ["cache", "media", "outputs"]:
            (self.base_dir / dir_name).mkdir(parents=True, exist_ok=True)

    @property
    def twitter_client(self) -> tweepy.Client:
        if self._twitter is None:
            token = os.environ.get("TWITTER_BEARER_TOKEN")
            if not token: raise RuntimeError("TWITTER_BEARER_TOKEN not set.")
            self._twitter = tweepy.Client(bearer_token=token, wait_on_rate_limit=False)
            if not self.args.offline:
                try:
                    self._twitter.get_user(username="twitterdev", user_fields=["id"])
                except tweepy.errors.TweepyException as e:
                    logger.warning(f"Could not verify Twitter client credentials: {e}")
        return self._twitter

    @property
    def reddit_client(self) -> praw.Reddit:
        if self._reddit is None:
            if not all(os.getenv(k) for k in ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"]):
                raise RuntimeError("Reddit credentials not fully set.")
            self._reddit = praw.Reddit(
                client_id=os.environ["REDDIT_CLIENT_ID"],
                client_secret=os.environ["REDDIT_CLIENT_SECRET"],
                user_agent=os.environ["REDDIT_USER_AGENT"],
                read_only=True,
            )
        return self._reddit

    @property
    def bluesky_client(self) -> Client:
        if self._bluesky is None:
            if not all(os.getenv(k) for k in ["BLUESKY_IDENTIFIER", "BLUESKY_APP_SECRET"]):
                raise RuntimeError("Bluesky credentials not set.")
            client = Client()
            if not self.args.offline:
                try:
                    client.login(os.environ["BLUESKY_IDENTIFIER"], os.environ["BLUESKY_APP_SECRET"])
                except atproto_exceptions.AtProtocolError as e:
                    logger.warning(f"Could not verify Bluesky client credentials: {e}")
            self._bluesky = client
        return self._bluesky

    def get_mastodon_clients(self) -> tuple[Dict[str, Mastodon], Optional[Mastodon]]:
        if not self._mastodon_clients_initialized:
            config_path_str = os.getenv("MASTODON_CONFIG_FILE", "mastodon_instances.json")
            config_path = self.base_dir / config_path_str if (self.base_dir / config_path_str).is_file() else Path(config_path_str)
            if not config_path.is_file():
                self._mastodon_clients_initialized = True
                return {}, None
            with open(config_path, "r", encoding="utf-8") as f:
                instances_config = json.load(f)
            for conf in instances_config:
                url, token = conf.get("api_base_url"), conf.get("access_token")
                if not url or not token: continue
                try:
                    client = Mastodon(access_token=token, api_base_url=url)
                    if not self.args.offline: client.instance()
                    self._mastodon_clients[url.rstrip('/')] = client
                    if conf.get("is_default_lookup_instance"):
                        self._default_mastodon_lookup_client = client
                except Exception as e:
                    logger.error(f"Failed to initialize Mastodon instance {url}: {e}")
            if not self._default_mastodon_lookup_client and self._mastodon_clients:
                self._default_mastodon_lookup_client = next(iter(self._mastodon_clients.values()))
            self._mastodon_clients_initialized = True
        return self._mastodon_clients, self._default_mastodon_lookup_client

    def get_platform_client(self, platform: str) -> Any:
        try:
            if platform == "twitter": return self.twitter_client
            if platform == "reddit": return self.reddit_client
            if platform == "bluesky": return self.bluesky_client
            if platform == "mastodon": return self.get_mastodon_clients()
        except (RuntimeError, tweepy.errors.TweepyException, praw.exceptions.PRAWException, atproto_exceptions.AtProtocolError) as e:
            raise RuntimeError(f"Failed to initialize client for {platform}: {e}")
        return None

    def _handle_purge(self):
        self.console.print("\n[bold yellow]Select Data to Purge:[/bold yellow]")
        options = {"1": ("All", ["cache", "media", "outputs"]), "2": ("Cache (Text/Metadata)", ["cache"]), "3": ("Media Files", ["media"]), "4": ("Output Reports", ["outputs"]), "5": ("Cancel", [])}
        for k, (n, _) in options.items(): self.console.print(f" {k}. {n}")
        choice = Prompt.ask("Enter number", default="5").strip()
        if choice not in options: self.console.print("[red]Invalid selection.[/red]"); return
        name, dirs = options[choice]
        if not dirs: self.console.print("[cyan]Purge operation cancelled.[/cyan]"); return
        if Confirm.ask(f"[bold red]This will PERMANENTLY delete all '{name}' data. Are you sure?[/bold red]", default=False):
            for d in dirs:
                path = self.base_dir / d
                if path.exists():
                    shutil.rmtree(path)
                    self.console.print(f"[green]Successfully purged '{path.name}'.[/green]")
                path.mkdir(parents=True, exist_ok=True)
        else:
            self.console.print("[cyan]Purge operation cancelled.[/cyan]")

    def _format_cache_age(self, timestamp_str: str) -> str:
        try:
            dt_obj = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
            return humanize.naturaltime(datetime.now(timezone.utc) - dt_obj)
        except (ValueError, TypeError): return "Invalid date"

    def _handle_cache_status(self):
        self.console.print("\n[bold cyan]Cache Status Overview:[/bold cyan]")
        cache_dir = self.base_dir / "cache"
        if not any(cache_dir.iterdir()): self.console.print("[yellow]No cache files found.[/yellow]\n"); return
        table = Table(title="Cached Data Summary", show_lines=True)
        table.add_column("Platform", style="cyan"); table.add_column("Username", style="magenta"); table.add_column("Last Fetched (UTC)", style="green", min_width=19, max_width=19); table.add_column("Age", style="yellow"); table.add_column("Items", style="blue", justify="right"); table.add_column("Media (A/F)", style="dim", justify="right")
        for file in sorted(cache_dir.glob("*.json")):
            try:
                platform, username = file.stem.split("_", 1)
                data = self.cache.load(platform, username)
                if not data: continue
                ts_str = data.get("timestamp", "N/A")
                age = self._format_cache_age(ts_str) if ts_str != "N/A" else "N/A"
                counts = {"twitter": f"{len(data.get('tweets',[]))}t", "reddit": f"{len(data.get('submissions',[]))}s, {len(data.get('comments',[]))}c", "bluesky": f"{len(data.get('posts',[]))}p", "mastodon": f"{len(data.get('posts',[]))}p", "hackernews": f"{len(data.get('items',[]))}i"}
                media_analyzed = len([m for m in data.get('media_analysis', []) if m and m.strip()])
                media_found = len(data.get('media_paths', []))
                media = f"{media_analyzed}/{media_found}"
                table.add_row(platform.capitalize(), username, ts_str[:19], age, counts.get(platform, "N/A"), media)
            except Exception as e: logger.error(f"Error processing {file.name} for status: {e}")
        self.console.print(table)

    def _get_cache_info_string(self, platform: str, username: str) -> str:
        data = self.cache.load(platform, username)
        if not data: return "[dim](no cache)[/dim]"
        ts = data.get("timestamp")
        fresh = "[red]date err[/red]"
        if ts:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            fresh = "[green]fresh[/green]" if age.total_seconds() < 24*3600 else f"[yellow]stale ({self._format_cache_age(ts)})[/yellow]"
        counts = {"twitter": len(data.get('tweets',[])), "reddit": len(data.get('submissions',[]))+len(data.get('comments',[])), "bluesky": len(data.get('posts',[])), "mastodon": len(data.get('posts',[])), "hackernews": len(data.get('items',[]))}
        return f"(cached: {counts.get(platform, 0)} items, {fresh})"

    def get_available_platforms(self, check_creds=True) -> List[str]:
        available = []
        if not check_creds or os.getenv("TWITTER_BEARER_TOKEN"): available.append("twitter")
        if not check_creds or all(os.getenv(k) for k in ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"]): available.append("reddit")
        if not check_creds or all(os.getenv(k) for k in ["BLUESKY_IDENTIFIER", "BLUESKY_APP_SECRET"]): available.append("bluesky")
        config_file = os.getenv("MASTODON_CONFIG_FILE", "mastodon_instances.json")
        if not check_creds or Path(config_file).is_file() or (self.base_dir / config_file).is_file(): available.append("mastodon")
        available.append("hackernews")
        return sorted(list(set(available)))

    def analyze(self, platforms: Dict[str, List[str]], query: str, force_refresh: bool = False, fetch_options: Optional[Dict[str, Any]] = None, console: Optional[Console] = None) -> str:
        collected_data: Dict[str, List[Dict]] = {p: [] for p in platforms}
        failed_fetches = []
        total_targets = sum(len(v) for v in platforms.values())
        
        # Use the provided console or the default one
        progress_console = console or self.console

        with Progress(SpinnerColumn(), "[progress.description]{task.description}", transient=True, console=progress_console) as progress:
            task = progress.add_task("[cyan]Collecting data...", total=total_targets)
            
            fetch_options = fetch_options or {}
            default_count = fetch_options.get("default_count", 50)

            for platform, usernames in platforms.items():
                fetcher = FETCHERS.get(platform)
                if not fetcher: failed_fetches.append((platform, "all", "N/A")); progress.advance(task, len(usernames)); continue
                for username in usernames:
                    progress.update(task, description=f"[cyan]Fetching {platform}/{username}...")
                    try:
                        client = self.get_platform_client(platform)
                        
                        target_key = f"{platform}:{username}"
                        target_opts = fetch_options.get("targets", {}).get(target_key, {})
                        limit = target_opts.get("count", default_count)

                        kwargs = {'username': username, 'cache': self.cache, 'llm': self.llm, 'force_refresh': force_refresh, 'fetch_limit': limit}
                        if platform == "mastodon": kwargs['clients'], kwargs['default_client'] = client
                        elif platform != "hackernews": kwargs['client'] = client
                        
                        data = fetcher(**kwargs)
                        if data: collected_data[platform].append({"username_key": username, "data": data})
                        else: failed_fetches.append((platform, username, "No data (check offline/cache)"))
                    except RateLimitExceededError as e:
                        handle_rate_limit(self.console, f"{platform.capitalize()} Fetch", e)
                        failed_fetches.append((platform, username, "Rate Limited"))
                    except (UserNotFoundError, AccessForbiddenError) as e:
                        failed_fetches.append((platform, username, str(e)))
                    except Exception as e:
                        logger.error(f"Fetch fail {platform}/{username}: {e}", exc_info=True)
                        failed_fetches.append((platform, username, "Unexpected Error"))
                    finally:
                        progress.advance(task)
                        
        if failed_fetches:
            self.console.print("[yellow]Data collection issues:[/yellow]")
            for p, u, r in failed_fetches: self.console.print(f"- {p}/{u}: {r}")
        if not any(collected_data.values()): return "[red]Data collection failed for all targets.[/red]"

        with self.console.status("[magenta]Analyzing with LLM..."):
            try:
                report = self.llm.run_analysis(collected_data, query)
            except RateLimitExceededError as e:
                handle_rate_limit(self.console, "LLM Analysis", e)
                return "[red]Analysis aborted due to LLM rate limit.[/red]"
            except Exception as e:
                 logger.error(f"LLM analysis failed: {e}", exc_info=True)
                 return f"[red]LLM analysis failed: {e}[/red]"

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text_model, img_model = os.getenv("ANALYSIS_MODEL"), os.getenv("IMAGE_ANALYSIS_MODEL")
        return f"# OSINT Analysis Report\n\n**Query:** {query}\n**Targets:** {', '.join(sorted(platforms.keys()))}\n**Generated:** {ts}\n**Mode:** {'Offline' if self.args.offline else 'Online'}\n**Models Used:**\n- Text: `{text_model}`\n- Image: `{img_model}`\n\n---\n\n{report}"

    def _save_output(self, content: str, query: str, platforms: List[str], format: str):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_q = "".join(c for c in query[:30] if c.isalnum() or c in " _-").strip() or "query"
        safe_p = "_".join(sorted(platforms)) or "platforms"
        base = f"analysis_{ts}_{safe_p}_{safe_q}"
        path = self.base_dir / "outputs" / f"{base}.{format}"
        if format == "json":
            md_lines = content.splitlines()
            metadata = { "query": query, "platforms": platforms }
            report_body_start_index = 0
            if md_lines and md_lines[0].startswith("#"):
                for i, line in enumerate(md_lines):
                    if line.strip() == "---":
                        report_body_start_index = i + 1
                        break
                    if ":" in line:
                        key, val = line.split(":", 1)
                        metadata[key.strip().lower().replace(" ", "_")] = val.strip().replace("`", "")
            report_body = "\n".join(md_lines[report_body_start_index:])
            data = {"analysis_metadata": metadata, "analysis_report_markdown": report_body}
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            path.write_text(content, encoding="utf-8")
        self.console.print(f"[green]Analysis saved to: {path}[/green]")

    def run(self):
        self.console.print(Panel("[bold blue]SocialOSINTAgent[/bold blue]\nCollects and analyzes user activity across multiple platforms using vision and LLMs.\nEnsure API keys and identifiers are set in your `.env` file or Mastodon JSON config.", title="Welcome", border_style="blue"))
        if self.args.offline: self.console.print(Panel("[bold yellow]OFFLINE MODE ENABLED[/bold yellow]\nData will be sourced only from local cache. No new data will be fetched or analyzed.", title_align="center", border_style="yellow"))
        
        while True:
            try:
                self.console.print("\n[bold cyan]Select Platform(s) for Analysis:[/bold cyan]")
                available = self.get_available_platforms(check_creds=True)
                if not available: self.console.print("[red]No platforms are configured correctly.[/red]"); break
                
                opts = {str(i+1): p for i,p in enumerate(available)}
                n = len(available)+1
                if len(available)>1: opts[str(n)]="cross-platform"; n+=1
                opts.update({str(n):"purge data", str(n+1):"cache status", str(n+2):"exit"})
                for k,v in opts.items(): self.console.print(f" {k}. {v.replace('-',' ').capitalize()}")
                
                choice = Prompt.ask("Enter number(s)", default=str(n+2))
                
                if opts.get(choice)=="exit": break
                if opts.get(choice)=="purge data": self._handle_purge(); continue
                if opts.get(choice)=="cache status": self._handle_cache_status(); continue
                
                selected = available if opts.get(choice)=="cross-platform" else [opts[k] for k in choice.split(',') if k.strip() in opts and opts[k.strip()] in available]
                if not selected: self.console.print("[yellow]Invalid selection.[/yellow]"); continue
                self.console.print(f"Selected: {', '.join(p.capitalize() for p in selected)}")
                
                query_platforms = {}
                for p in selected:
                    prompt_msg = f"Enter {p.capitalize()} username(s) (comma-separated"
                    if p == "twitter": prompt_msg += ", no '@')"
                    elif p == "reddit": prompt_msg += ", no 'u/')"
                    elif p == "bluesky": prompt_msg += ", e.g., 'handle.bsky.social')"
                    elif p == "mastodon": prompt_msg += ", format: 'user@instance.domain')"
                    else: prompt_msg += ")"
                    if self.args.offline: prompt_msg += " - OFLINE, cache only)"
                    
                    users_input = Prompt.ask(prompt_msg)
                    if not users_input: continue
                    users = [sanitize_username(u.strip()) for u in users_input.split(',') if u.strip()]
                    if users:
                        self.console.print(Text("Cache check: ", style="dim") + Text.from_markup(", ".join([f"{u} {self._get_cache_info_string(p,u)}" for u in users])))
                        query_platforms[p] = users
                if not query_platforms: self.console.print("[yellow]No users entered.[/yellow]"); continue
                
                default_count_str = Prompt.ask("Enter default number of items to fetch per target", default="50")
                try:
                    default_count = int(default_count_str)
                except ValueError:
                    default_count = 50
                    self.console.print("[yellow]Invalid number, using 50.[/yellow]")
                
                fetch_options = {"default_count": default_count, "targets": {}}
                
                self._run_analysis_loop(query_platforms, fetch_options)
            
            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[yellow]Operation cancelled.[/yellow]")
                if Confirm.ask("Exit program?", default=False):
                    break
                else:
                    continue
    
    def _handle_loadmore_command(self, parts: List[str], platforms: Dict[str, List[str]], fetch_options: Dict[str, Any], last_query: str) -> Tuple[bool, str, bool]:
        """
        Handles all logic for the 'loadmore' command.

        Returns:
            Tuple[bool, str, bool]: A tuple of (should_run_analysis, query_to_run, force_refresh)
        """
        # --- 1. Basic command structure validation ---
        if len(parts) not in [2, 3]:
            self.console.print("[red]Invalid `loadmore` format. Use: `loadmore <count>` or `loadmore <platform/user> <count>`[/red]")
            return False, "", False

        # --- 2. Parse arguments ---
        target_str, count_str = (parts[1], parts[2]) if len(parts) == 3 else (None, parts[1])

        # --- 3. Validate count ---
        try:
            count_to_add = int(count_str)
        except ValueError:
            self.console.print(f"[red]Invalid count: '{count_str}'. Must be a number.[/red]")
            return False, "", False

        # --- 4. Resolve target if not explicitly provided ---
        if not target_str:
            all_targets = [f"{p}/{u}" for p, users in platforms.items() for u in users]
            if len(all_targets) == 1:
                target_str = all_targets[0]
            elif len(all_targets) > 1:
                self.console.print("[cyan]Multiple targets active. Please choose one:[/cyan]")
                prompt_choices = {str(i): t for i, t in enumerate(all_targets, 1)}
                for i_str, t in prompt_choices.items(): self.console.print(f" {i_str}. {t}")
                choice = Prompt.ask("Enter number", choices=list(prompt_choices.keys()), show_choices=False)
                target_str = prompt_choices.get(choice)
            else:
                self.console.print("[red]Error: No active targets in the session.[/red]")
                return False, "", False
        
        if not target_str: # User cancelled the prompt
            return False, "", False

        # --- 5. Validate final target string ---
        try:
            platform, username = target_str.split('/', 1)
        except ValueError:
            self.console.print(f"[red]Invalid target format: '{target_str}'. Expected 'platform/username'.[/red]")
            return False, "", False

        if platform not in platforms or username not in platforms.get(platform, []):
            self.console.print(f"[red]Error: Target '{target_str}' is not part of the current session.[/red]")
            return False, "", False

        # --- 6. Update fetch options ---
        target_key = f"{platform}:{username}"
        current_count = fetch_options.get("targets", {}).get(target_key, {}).get("count", fetch_options.get("default_count", 50))
        new_count = current_count + count_to_add
        
        if "targets" not in fetch_options: fetch_options["targets"] = {}
        if target_key not in fetch_options["targets"]: fetch_options["targets"][target_key] = {}
        fetch_options["targets"][target_key]["count"] = new_count
        
        # --- 7. Decide next action ---
        if last_query:
            self.console.print(f"[cyan]Fetch plan updated for {target_str} to {new_count} items. Re-running last query...[/cyan]")
            return True, last_query, True
        else:
            self.console.print(f"[cyan]Fetch plan updated for {target_str} to {new_count} items. Please enter an analysis query.[/cyan]")
            return False, "", False

    def _run_analysis_loop(self, platforms: Dict[str, List[str]], fetch_options: Dict[str, Any]):
        platform_info = " | ".join([f"{p.capitalize()}: {', '.join(u)}" for p, u in platforms.items()])
        self.console.print(Panel(f"Targets: {platform_info}\nCommands: `exit`, `refresh`, `help`, `loadmore [<platform/user>] <count>`", title="🔎 Analysis Session", border_style="cyan", expand=False))
        last_query = ""
        while True:
            try:
                user_input = Prompt.ask("\n[bold green]Analysis Query>[/bold green]", default=last_query).strip()
                if not user_input: continue

                parts = user_input.split()
                command = parts[0].lower() if parts else ""

                force_refresh = False
                should_run_analysis = False
                query_to_run = ""

                # --- COMMAND ROUTER ---
                if command == "exit":
                    break
                elif command == "help":
                    self.console.print(Panel("`exit`: Return to menu.\n`refresh`: Force full data fetch.\n`loadmore <count>`: Add items for the sole target, or choose from a list.\n`loadmore <platform/user> <count>`: Explicitly add items for a target (e.g., `loadmore twitter/user001 100`).\n`help`: Show this message.", title="Help"))
                    continue
                elif command == "refresh":
                    if self.args.offline:
                        self.console.print("[yellow]'refresh' is unavailable in offline mode.[/yellow]")
                        continue
                    if Confirm.ask("Force refresh data for all targets? This uses more API calls.", default=False):
                        force_refresh = True
                        query_to_run = Prompt.ask("Enter analysis query for refreshed data", default=last_query if last_query != "refresh" else "").strip()
                        if query_to_run:
                            should_run_analysis = True
                        else:
                            self.console.print("[yellow]Refresh cancelled, no query entered.[/yellow]")
                    continue
                elif command == "loadmore":
                    should_run_analysis, query_to_run, force_refresh = self._handle_loadmore_command(
                        parts, platforms, fetch_options, last_query
                    )
                else: # Not a recognized command, so it's a query
                    query_to_run = user_input
                    should_run_analysis = True

                # --- ANALYSIS EXECUTION ---
                if not should_run_analysis:
                    continue
                
                last_query = query_to_run
                result = self.analyze(platforms, query_to_run, force_refresh, fetch_options)
                
                is_error = result.strip().lower().startswith("[red]")
                border_color = "red" if is_error else "green"
                content_to_render = Text.from_markup(result) if is_error else Markdown(result)
                self.console.print(Panel(content_to_render, title="Analysis Report", border_style=border_color, expand=True))
                
                if not is_error:
                    if not self.args.no_auto_save:
                        self._save_output(result, query_to_run, list(platforms.keys()), self.args.format)
                    elif Confirm.ask("Save report?"):
                        save_format = Prompt.ask("Format?", choices=["markdown", "json"], default=self.args.format)
                        self._save_output(result, query_to_run, list(platforms.keys()), save_format)

            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[yellow]Analysis query cancelled.[/yellow]")
                if Confirm.ask("\nExit this analysis session (return to menu)?", default=False):
                    break
                else:
                    last_query = ""
                    continue
            except Exception as e:
                logger.error(f"Error in analysis loop: {e}", exc_info=True)
                self.console.print(f"[bold red]An error occurred: {e}[/bold red]")

    def process_stdin(self):
        stderr = Console(stderr=True)
        stderr.print("[cyan]Processing analysis request from stdin...[/cyan]")
        try:
            data = json.load(sys.stdin)
            platforms, query = data.get("platforms"), data.get("query")
            fetch_options = data.get("fetch_options") # New: read fetch options
            if not isinstance(platforms, dict) or not platforms or not isinstance(query, str) or not query.strip():
                raise ValueError("Invalid JSON: 'platforms' (dict) and 'query' (str) required.")
            query_platforms = {p: [sanitize_username(u.strip()) for u in (us if isinstance(us,list) else [us]) if u.strip()] for p, us in platforms.items() if p in self.get_available_platforms()}
            if not query_platforms: raise ValueError("No valid/configured platforms found in input.")
            
            # Pass the stderr console and fetch_options to analyze()
            report = self.analyze(query_platforms, query, fetch_options=fetch_options, console=stderr)
            
            if not report.strip().lower().startswith("[red]"):
                if self.args.no_auto_save: 
                    # Print final report to standard output
                    print(report)
                else: 
                    self._save_output(report, query, list(query_platforms.keys()), self.args.format)
                sys.exit(0)
            else:
                sys.stderr.write(f"Analysis Error:\n{report}\n")
                sys.exit(2)
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"Error: {e}\n"); sys.exit(1)