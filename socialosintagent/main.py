import argparse
import logging
import sys

from rich.console import Console

from socialosintagent.analyzer import SocialOSINTAgent

def main():
    parser = argparse.ArgumentParser(
        description="Social Media OSINT analyzer using LLMs. Fetches user data from various platforms, performs text and image analysis, and generates reports.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Environment Variables Required for LLM (using OpenAI-compatible API):
  LLM_API_KEY             : API key for your chosen LLM provider.
  LLM_API_BASE_URL        : Base URL for the LLM API endpoint.
  IMAGE_ANALYSIS_MODEL    : Vision model name recognized by your LLM provider/endpoint.
  ANALYSIS_MODEL          : Text model name recognized by your LLM provider/endpoint.

Optional for OpenRouter:
  OPENROUTER_REFERER      : Your site URL or app name.
  OPENROUTER_X_TITLE      : Your project name.

Platform Credentials (at least one set required, or use HackerNews / Mastodon config):
  TWITTER_BEARER_TOKEN    : Twitter API v2 Bearer Token.
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
  BLUESKY_IDENTIFIER, BLUESKY_APP_SECRET
  
Mastodon Configuration:
  MASTODON_CONFIG_FILE    : Path to a JSON file for Mastodon instance configurations.
                            (Default: "mastodon_instances.json")

Place these in a `.env` file or set them in your environment.
""",
    )
    parser.add_argument("--stdin", action="store_true", help="Read analysis request from stdin as JSON.")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format for saving analysis reports.")
    parser.add_argument("--no-auto-save", action="store_true", help="Disable automatic saving of reports.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="WARNING", help="Set the logging level.")
    parser.add_argument("--offline", action="store_true", help="Run in offline mode, using only cached data.")
    
    args = parser.parse_args()

    # Configure logging
    log_level_numeric = getattr(logging, args.log_level.upper())
    logging.basicConfig(
        level=log_level_numeric,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler("analyzer.log"), logging.StreamHandler()],
    )
    logging.getLogger("SocialOSINTAgent").setLevel(log_level_numeric)
    
    if args.offline:
        logging.info("Running in OFFLINE mode.")

    try:
        analyzer_instance = SocialOSINTAgent(args)
        if args.stdin:
            analyzer_instance.process_stdin()
        else:
            analyzer_instance.run()
    except RuntimeError as e:
        error_console = Console(stderr=True, style="bold red")
        error_console.print(f"\nCRITICAL ERROR: {e}")
        error_console.print("Ensure necessary API keys and platform credentials/URLs are correctly set.")
        sys.exit(1)
    except Exception as e:
        logging.getLogger("SocialOSINTAgent").critical(f"An unexpected critical error occurred: {e}", exc_info=True)
        error_console = Console(stderr=True, style="bold red")
        error_console.print(f"\nUNEXPECTED CRITICAL ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()