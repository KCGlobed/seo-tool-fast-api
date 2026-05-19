from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from enum import Enum
import sqlite3
import httpx
from bs4 import BeautifulSoup
import urllib.parse
import random
import re
from typing import List, Optional
from datetime import datetime, timedelta
import logging
import os

# Configure application logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("seo_rank_tracker")

app = FastAPI(
    title="SEO Keyword Rank Tracker API",
    description="A high-performance FastAPI service to crawl Google organic results and compute website rankings.",
    version="1.0.0"
)

# Configure CORS so any local or remote frontend can connect seamlessly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for development flexibility
    allow_credentials=True,
    allow_methods=["*"],  # Allows all HTTP methods (GET, POST, OPTIONS, DELETE, etc.)
    allow_headers=["*"],
)

DATABASE_FILE = "history.db"

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            keyword TEXT NOT NULL,
            rank INTEGER NOT NULL,
            url TEXT,
            title TEXT,
            depth INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    
    # Run dynamic column migrations for Google Search Console integrations
    cursor.execute("PRAGMA table_info(history)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "source" not in columns:
        cursor.execute("ALTER TABLE history ADD COLUMN source TEXT DEFAULT 'scraper'")
    if "clicks" not in columns:
        cursor.execute("ALTER TABLE history ADD COLUMN clicks INTEGER DEFAULT 0")
    if "impressions" not in columns:
        cursor.execute("ALTER TABLE history ADD COLUMN impressions INTEGER DEFAULT 0")
    if "ctr" not in columns:
        cursor.execute("ALTER TABLE history ADD COLUMN ctr REAL DEFAULT 0.0")
        
    conn.commit()
    conn.close()

# Initialize DB on startup
init_db()

# --- PYDANTIC SCHEMAS ---

class RankSource(str, Enum):
    scraper = "scraper"
    mock = "mock"
    gsc = "gsc"

class RankCheckRequest(BaseModel):
    domain: str = Field(..., example="example.com", description="The target website domain to search for")
    keyword: str = Field(..., example="cloud hosting solutions", description="The search term to query on Google")
    depth: int = Field(50, ge=10, le=100, description="The depth of search results to scan (e.g., top 10 to 100)")
    use_mock: bool = Field(False, description="Simulate Google rankings. Highly useful for frontend testing and avoiding search blockages")
    source: RankSource = Field(RankSource.scraper, description="Ranking source to use: 'scraper', 'mock', or 'gsc'")
    gsc_credentials_path: Optional[str] = Field("seo-tool-496811-52289d0c0e56.json", description="Local path to Google Service Account JSON Key file")
    gsc_property_url: Optional[str] = Field(None, description="Exact Search Console property URL (e.g., 'https://www.example.com/' or 'sc-domain:example.com'). If null, defaults to normal domain format.")

class RankCheckResponse(BaseModel):
    id: Optional[int] = None
    domain: str
    keyword: str
    rank: int  # 0 represents 'Not Found' in the scanned depth
    found: bool
    url: Optional[str] = None
    title: Optional[str] = None
    depth: int
    timestamp: str
    is_mock: bool = False
    source: RankSource = RankSource.scraper
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0

class HistoryItem(BaseModel):
    id: int
    domain: str
    keyword: str
    rank: int
    url: Optional[str]
    title: Optional[str]
    depth: int
    timestamp: str
    source: RankSource = RankSource.scraper
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0

class DeleteResponse(BaseModel):
    success: bool
    message: str

# --- SCRAPER & DOMAIN PARSER LOGIC ---

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0"
]

def get_random_headers():
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1"
    }
    return headers

def normalize_domain(domain_str: str) -> str:
    """Removes protocol, www subdomain, and trailing directories or slashes."""
    # Remove protocol
    domain_str = re.sub(r"^https?://", "", domain_str, flags=re.IGNORECASE)
    # Remove www.
    domain_str = re.sub(r"^www\.", "", domain_str, flags=re.IGNORECASE)
    # Extract only host name before slash
    domain_str = domain_str.split("/")[0]
    return domain_str.lower().strip()

def clean_google_url(url: str) -> Optional[str]:
    """Cleans redirects and handles Google's link wrapper, ignoring administrative pages."""
    if not url:
        return None
        
    # Handle /url?q= redirection wrapper (sometimes served by Google to simple client scrapes)
    if url.startswith("/url?q="):
        parsed = urllib.parse.urlparse(url)
        queries = urllib.parse.parse_qs(parsed.query)
        if "q" in queries:
            url = queries["q"][0]
            
    if not url.startswith("http"):
        return None
        
    try:
        parsed_res = urllib.parse.urlparse(url)
        netloc = parsed_res.netloc.lower()
        
        # Sift out administrative and Google internal domains
        ignored_domains = [
            "google.com", "google.co.in", "support.google.com", "accounts.google.com",
            "maps.google.com", "news.google.com", "play.google.com", "translate.google.com",
            "webcache.googleusercontent.com", "policies.google.com", "youtube.com", "www.youtube.com"
        ]
        
        for ig in ignored_domains:
            if netloc == ig or netloc.endswith("." + ig):
                return None
                
        return url
    except Exception:
        return None

def domain_matches(target_domain: str, result_url: str) -> bool:
    """Checks if the result_url matches the target domain or its subdomains."""
    target_norm = normalize_domain(target_domain)
    cleaned_url = clean_google_url(result_url)
    if not cleaned_url:
        return False
        
    try:
        parsed_res = urllib.parse.urlparse(cleaned_url)
        res_host = parsed_res.netloc.lower()
        if res_host.startswith("www."):
            res_host = res_host[4:]
            
        # Matches exactly or is a sub-domain of the target (e.g. blog.example.com vs example.com)
        return res_host == target_norm or res_host.endswith("." + target_norm)
    except Exception:
        return False

async def get_gsc_rank_position(
    credentials_path: str,
    domain: str,
    keyword: str,
    gsc_property_url: Optional[str] = None,
    days_back: int = 3
) -> dict:
    """
    Queries Google Search Console Search Analytics API server-to-server to fetch average position, clicks, impressions, and CTR.
    """
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests
        import google.auth
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Google authorization libraries are not installed in the active Python environment. "
                   "Please run: source venv/bin/activate && pip install -r requirements.txt"
        )

    # Normalize GSC Property URL
    property_url = gsc_property_url
    if not property_url:
        # Standard GSC domain property format
        property_url = f"sc-domain:{normalize_domain(domain)}"
        logger.info("GSC: Auto-normalizing domain '%s' to property URL '%s'", domain, property_url)
    else:
        property_url = property_url.strip()

    try:
        # 1. Load service account credentials (local key file or keyless Application Default Credentials)
        scopes = ["https://www.googleapis.com/auth/webmasters.readonly"]
        
        if credentials_path and os.path.exists(credentials_path):
            creds = service_account.Credentials.from_service_account_file(
                credentials_path, scopes=scopes
            )
            logger.info("GSC: Authenticated using local service account JSON key: %s", credentials_path)
        else:
            # Check if running inside Google Cloud Platform (e.g. Cloud Run) or if local ADC environment variable is defined
            is_gcp_or_adc = os.getenv("K_SERVICE") is not None or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") is not None
            
            if is_gcp_or_adc:
                logger.info("GSC: Local key file not found. Falling back to Application Default Credentials (ADC)...")
                try:
                    creds, project_id = google.auth.default(scopes=scopes)
                except Exception as e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Google Search Console credentials not found. Local JSON file at '{credentials_path}' "
                               f"does not exist, and Application Default Credentials (ADC) failed: {str(e)}."
                    )
            else:
                # Local environment with no JSON file and no ADC environment variables configured
                raise HTTPException(
                    status_code=400,
                    detail=f"Google Search Console credentials JSON file not found at '{credentials_path}'. "
                           f"For local development, please place your Service Account JSON key at this path."
                )
        
        # 2. Refresh credentials to get access token (no browser redirect required!)
        auth_request = google.auth.transport.requests.Request()
        creds.refresh(auth_request)
        
        # 3. Calculate query date range (GSC has ~2-3 days data processing latency)
        end_date = datetime.utcnow().strftime("%Y-%m-%d")
        start_date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        
        # Encode property URL for standard REST query path
        encoded_property = urllib.parse.quote_plus(property_url)
        gsc_api_url = f"https://www.googleapis.com/webmasters/v3/sites/{encoded_property}/searchAnalytics/query"
        
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json"
        }
        
        request_body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": ["query"],
            "dimensionFilterGroups": [
                {
                    "filters": [
                        {
                            "dimension": "query",
                            "operator": "equals",
                            "expression": keyword
                        }
                    ]
                }
            ],
            "rowLimit": 1
        }
        
        logger.info("GSC API Request: Querying %s for query '%s'", gsc_api_url, keyword)
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(gsc_api_url, json=request_body, headers=headers)
            
            if response.status_code == 403:
                # Handle common permissions issues gracefully
                error_info = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
                logger.error("GSC 403 Permission Denied: %s", error_info)
                raise HTTPException(
                    status_code=403,
                    detail=f"Google Search Console permissions error for property '{property_url}'. "
                           f"Ensure your Service Account email ({creds.service_account_email}) "
                           f"has been added as a Viewer or Owner in your Google Search Console settings."
                )
                
            if response.status_code != 200:
                logger.error("GSC API error %d: %s", response.status_code, response.text)
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Google Search Console API error: {response.text}"
                )
                
            return response.json()
            
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error authenticating or querying GSC: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to query Google Search Console: {str(e)}"
        )

# --- CORE API ROUTINGS ---

@app.get("/api/status")
def get_status():
    """Simple status check to verify FastAPI is healthy and running."""
    return {"status": "online", "message": "SEO Keyword Rank Tracker API is fully operational"}

@app.post("/api/rank", response_model=RankCheckResponse)
async def check_keyword_rank(payload: RankCheckRequest):
    """
    Computes keyword rankings and search performance. Supports live HTML scraping, 
    simulated mock tests, and official Google Search Console (GSC) server-to-server queries.
    """
    domain = payload.domain.strip()
    keyword = payload.keyword.strip()
    depth = payload.depth
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Guard check for empty params
    if not domain or not keyword:
        raise HTTPException(status_code=400, detail="Domain and Keyword parameters cannot be empty")

    # Determine final rank source (backward compatible with use_mock toggle)
    source = payload.source
    if payload.use_mock:
        source = RankSource.mock

    # Initialize GSC parameters
    clicks = 0
    impressions = 0
    ctr = 0.0

    if source == RankSource.mock:
        # MOCK BEHAVIOR: Generates simulated search ranking for reliable development testing
        found_chance = random.random()
        if found_chance > 0.85:
            final_rank = 0
            matched_url = None
            matched_title = None
        else:
            final_rank = random.randint(1, depth)
            keyword_slug = urllib.parse.quote(keyword.lower().replace(" ", "-"))
            matched_url = f"https://www.{normalize_domain(domain)}/{keyword_slug}"
            matched_title = f"{keyword.capitalize()} - Best Services & Guides | {domain.split('.')[0].upper()}"
            
            # Simulate realistic search performance stats
            clicks = random.randint(5, 120)
            impressions = random.randint(150, 2000)
            ctr = round((clicks / impressions) * 100, 2) if impressions > 0 else 0.0

        logger.info("Mock rank computed: rank=%d for keyword='%s' and domain='%s'", final_rank, keyword, domain)

    elif source == RankSource.gsc:
        # GOOGLE SEARCH CONSOLE API FLOW (Server-to-Server, Non-Interactive)
        gsc_data = await get_gsc_rank_position(
            credentials_path=payload.gsc_credentials_path,
            domain=domain,
            keyword=keyword,
            gsc_property_url=payload.gsc_property_url
        )
        
        rows = gsc_data.get("rows", [])
        if rows:
            first_row = rows[0]
            raw_position = first_row.get("position", 0.0)
            # GSC average position can be a float. Round to nearest integer.
            final_rank = int(round(raw_position))
            clicks = int(first_row.get("clicks", 0))
            impressions = int(first_row.get("impressions", 0))
            # Convert decimal ratio (e.g. 0.053) to percentage (e.g. 5.3)
            ctr = round(float(first_row.get("ctr", 0.0)) * 100, 2)
            matched_url = f"https://{normalize_domain(domain)}"
            matched_title = "Google Search Console Performance Report"
            
            logger.info("GSC rank fetched successfully: position=%.2f, clicks=%d, impressions=%d", raw_position, clicks, impressions)
        else:
            final_rank = 0
            matched_url = None
            matched_title = None
            logger.info("GSC fetched empty performance data for query '%s' on domain '%s'", keyword, domain)

    else:
        # REAL SEARCH SCRAPING FLOW
        google_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(keyword)}&num={depth}&gbv=1"
        headers = get_random_headers()
        
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(google_url, headers=headers)
                
                if response.status_code == 429:
                    raise HTTPException(
                        status_code=429, 
                        detail="Google is rate-limiting search queries. Try enabling 'use_mock' to test the application or wait a few minutes."
                    )
                elif response.status_code != 200:
                    raise HTTPException(
                        status_code=502, 
                        detail=f"Google Search returned unexpected status: {response.status_code}"
                    )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=503, 
                detail=f"Failed to reach Google Search engine: {str(e)}"
            )

        # Check if we were blocked by Google's anti-bot/JS challenge
        is_blocked = "enablejs" in response.text or "/httpservice/retry" in response.text or "captcha" in response.text.lower()
        
        if is_blocked:
            raise HTTPException(
                status_code=429,
                detail="Google has blocked this request with a JavaScript challenge/CAPTCHA. Please enable the 'use_mock' parameter to simulate the search rankings, or run the server on a residential IP."
            )

        # PARSE HTML ORGANIC RESULTS
        soup = BeautifulSoup(response.text, "html.parser")
        parsed_results = []
        
        # Selection Strategy 1: Anchor tags containing h3 headers (standard desktop search results structure)
        for a in soup.find_all("a"):
            href = a.get("href", "")
            h3 = a.find("h3")
            if href and h3:
                cleaned = clean_google_url(href)
                if cleaned and cleaned not in [r["url"] for r in parsed_results]:
                    title = h3.get_text(strip=True)
                    parsed_results.append({"url": cleaned, "title": title})

        # Selection Strategy 2: Google result card selector (.g, .yuRUbf)
        if not parsed_results:
            for div in soup.select("div.g, div.yuRUbf"):
                a = div.find("a")
                if a and a.get("href"):
                    cleaned = clean_google_url(a.get("href"))
                    if cleaned and cleaned not in [r["url"] for r in parsed_results]:
                        h3 = div.find("h3")
                        title = h3.get_text(strip=True) if h3 else "Organic Result"
                        parsed_results.append({"url": cleaned, "title": title})

        # Selection Strategy 3: Exhaustive container fallback on links
        if not parsed_results:
            search_container = soup.find(id="search") or soup.find(id="main")
            if search_container:
                for a in search_container.find_all("a"):
                    href = a.get("href", "")
                    cleaned = clean_google_url(href)
                    if cleaned and cleaned not in [r["url"] for r in parsed_results]:
                        title = a.get_text(strip=True)
                        # Filter out short or empty anchor tags that are likely administrative utilities
                        if len(title) > 8:
                            parsed_results.append({"url": cleaned, "title": title})

        logger.info("Scraper found %d organic results from Google for keyword '%s'", len(parsed_results), keyword)

        # COMPUTE RANK (1-indexed position)
        final_rank = 0
        matched_url = None
        matched_title = None

        for index, res in enumerate(parsed_results, start=1):
            if index > depth:
                break
            if domain_matches(domain, res["url"]):
                final_rank = index
                matched_url = res["url"]
                matched_title = res["title"]
                break

    # SAVE RESULT TO SQLITE HISTORY
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO history (domain, keyword, rank, url, title, depth, timestamp, source, clicks, impressions, ctr) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (domain, keyword, final_rank, matched_url, matched_title, depth, timestamp, source.value, clicks, impressions, ctr)
    )
    last_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return RankCheckResponse(
        id=last_id,
        domain=domain,
        keyword=keyword,
        rank=final_rank,
        found=final_rank > 0,
        url=matched_url,
        title=matched_title,
        depth=depth,
        timestamp=timestamp,
        is_mock=(source == RankSource.mock),
        source=source,
        clicks=clicks,
        impressions=impressions,
        ctr=ctr
    )

@app.get("/api/history", response_model=List[HistoryItem])
def get_search_history():
    """
    Fetches the history of search ranking queries, ordered from newest to oldest.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, domain, keyword, rank, url, title, depth, timestamp, source, clicks, impressions, ctr 
        FROM history ORDER BY id DESC
        """
    )
    rows = cursor.fetchall()
    
    history_items = []
    for r in rows:
        history_items.append(
            HistoryItem(
                id=r["id"],
                domain=r["domain"],
                keyword=r["keyword"],
                rank=r["rank"],
                url=r["url"],
                title=r["title"],
                depth=r["depth"],
                timestamp=r["timestamp"],
                source=RankSource(r["source"]) if r["source"] else RankSource.scraper,
                clicks=r["clicks"] if r["clicks"] is not None else 0,
                impressions=r["impressions"] if r["impressions"] is not None else 0,
                ctr=r["ctr"] if r["ctr"] is not None else 0.0
            )
        )
    conn.close()
    return history_items

@app.delete("/api/history/{item_id}", response_model=DeleteResponse)
def delete_history_item(item_id: int):
    """
    Removes a single search query record from the SQLite database history.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    # Check if record exists
    cursor.execute("SELECT id FROM history WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="History log item not found")
        
    cursor.execute("DELETE FROM history WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    
    return DeleteResponse(success=True, message=f"Log item {item_id} successfully deleted.")
