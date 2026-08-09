"""Search / editor / geo MCP adapters."""
from apitelegramchat.search_engine import (
    execute_book_lookup, execute_crypto_price, execute_done, execute_distance, execute_elevation,
    execute_exchange_rate, execute_fetch_url, execute_geocode, execute_generate_image,
    execute_generate_video, execute_hacker_news, execute_image_search, execute_ip_geo,
    execute_isochrone, execute_news, execute_place_details, execute_qr_code, execute_route,
    execute_search_poi, execute_text_editor, execute_weather, execute_web_search, execute_wikipedia,
)
__all__ = [name for name in globals() if name.startswith("execute_")]
