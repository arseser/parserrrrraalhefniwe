from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx
from datetime import datetime, timezone
from typing import Optional
import re
import os

app = FastAPI(title="OLX.bg Parser")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
}

OLX_API = "https://www.olx.bg/api/v1/offers/"


async def fetch_offers(query: str = "", limit: int = 40):
    params = {
        "offset": 0,
        "limit": min(limit, 50),
        "query": query,
        "sort_by": "created_at:desc",
    }
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
        r = await client.get(OLX_API, params=params)
        r.raise_for_status()
        data = r.json()
    return data.get("data", [])


async def fetch_seller_reg_date(user_id: int) -> Optional[datetime]:
    """Пытается вытащить дату регистрации со страницы профиля продавца."""
    url = f"https://www.olx.bg/ads/user/{user_id}/"
    try:
        async with httpx.AsyncClient(timeout=12, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            html = r.text
        # Паттерн 1: текст "Регистриран на DD.MM.YYYY"
        m = re.search(r'Регистриран на\s*(\d{2}\.\d{2}\.\d{4})', html)
        if m:
            return datetime.strptime(m.group(1), "%d.%m.%Y")
        # Паттерн 2: JSON-LD dateCreated
        m = re.search(r'"dateCreated"\s*:\s*"([^"]+)"', html)
        if m:
            return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except Exception:
        return None
    return None


def format_date(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d-%m-%Y в %H:%M")


def days_ago(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    if delta.days > 0:
        return f"{delta.days} дн. назад"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours} ч. назад"
    return "только что"


@app.get("/api/search")
async def search(
    q: str = "",
    limit: int = 30,
    min_seller_year: Optional[int] = Query(None, description="Минимальный год регистрации продавца"),
    max_seller_year: Optional[int] = Query(None, description="Максимальный год регистрации продавца"),
    max_listing_age_days: Optional[int] = Query(None, description="Максимальный возраст объявления в днях"),
):
    raw = await fetch_offers(query=q, limit=limit)
    results = []
    now = datetime.now(timezone.utc)

    for item in raw:
        created_str = item.get("created_time") or item.get("last_refresh_time")
        created_dt = None
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except Exception:
                pass

        # Фильтр по свежести объявления
        if max_listing_age_days is not None and created_dt:
            if (now - created_dt).days > max_listing_age_days:
                continue

        user_id = item.get("user_id")
        seller_reg = None
        if user_id:
            seller_reg = await fetch_seller_reg_date(user_id)

        # Фильтр по году регистрации продавца
        if min_seller_year or max_seller_year:
            if seller_reg is None:
                continue
            if min_seller_year and seller_reg.year < min_seller_year:
                continue
            if max_seller_year and seller_reg.year > max_seller_year:
                continue

        ad_url = item.get("url") or f"https://www.olx.bg/d/ad/{item.get('id')}.html"
        seller_url = f"https://www.olx.bg/ads/user/{user_id}/" if user_id else ""
        chat_url = f"https://www.olx.bg/d/ad/{item.get('id')}.html#chat"

        photo_url = ""
        photos = item.get("photos") or []
        if photos:
            photo_url = photos[0].get("link") or photos[0].get("url") or ""

        cats = item.get("category") or []
        category_name = cats[-1].get("name") if cats else "—"

        results.append({
            "country": "🇵🇹",
            "category": category_name,
            "title": item.get("title", ""),
            "price": item.get("price", {}).get("display_value", "—"),
            "published": format_date(created_dt),
            "published_ago": days_ago(created_dt),
            "delivery": "Да" if item.get("delivery") else "",
            "seller_name": item.get("user_name", "—"),
            "seller_ads_count": "—",
            "seller_reg": seller_reg.strftime("%d-%m-%Y") if seller_reg else "—",
            "ad_url": ad_url,
            "seller_url": seller_url,
            "chat_url": chat_url,
            "photo_url": photo_url,
            "views": item.get("views", 0),
        })

    return results


# Отдача статического фронтенда
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
