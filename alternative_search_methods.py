#!/usr/bin/env python3
"""
Alternative search methods for spec sheet discovery when Google searches are blocked.
This module provides several backup approaches for finding equipment specification sheets.
"""

import requests
import json
import logging
from typing import List, Dict, Optional

def search_with_bing_api(make: str, model: str, api_key: str, max_results: int = 10) -> List[Dict]:
    """
    Search for spec sheets using Bing Search API (requires API key).
    
    Args:
        make: Equipment manufacturer
        model: Equipment model
        api_key: Bing Search API key
        max_results: Maximum number of results to return
        
    Returns:
        List of search result dictionaries
    """
    if not api_key:
        logging.warning("Bing API key not provided")
        return []
    
    # Create search query
    search_query = f'"{make}" "{model}" specification sheet manual datasheet technical documentation'
    
    try:
        # Bing Search API endpoint
        endpoint = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        params = {
            "q": search_query,
            "count": max_results,
            "mkt": "en-US"
        }
        
        response = requests.get(endpoint, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        search_results = []
        
        for item in data.get("webPages", {}).get("value", []):
            search_results.append({
                "title": item.get("name", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet", "")
            })
        
        logging.info(f"Bing search returned {len(search_results)} results for {make} {model}")
        return search_results
        
    except Exception as e:
        logging.warning(f"Bing search failed for {make} {model}: {e}")
        return []


def search_manufacturer_direct(make: str, model: str) -> List[Dict]:
    """
    Search for spec sheets by constructing direct manufacturer URLs.
    
    Args:
        make: Equipment manufacturer
        model: Equipment model
        
    Returns:
        List of potential manufacturer URLs
    """
    manufacturer_urls = {
        "caterpillar": "https://www.cat.com/en_US/products/new/power-systems/industrial-engines/",
        "ge": "https://www.ge.com/gas-power/products/gas-turbines/",
        "cleaver brooks": "https://www.cleaverbrooks.com/products/boilers/",
        "john deere": "https://www.deere.com/en/engines/",
        "cummins": "https://www.cummins.com/engines/",
        "detroit diesel": "https://www.detroitdiesel.com/engines/",
        "perkins": "https://www.perkins.com/en_US/products/engines/",
        "kohler": "https://www.kohlerpower.com/engines/",
        "briggs & stratton": "https://www.briggsandstratton.com/engines/",
        "honda": "https://engines.honda.com/"
    }
    
    make_lower = make.lower().strip()
    
    if make_lower in manufacturer_urls:
        base_url = manufacturer_urls[make_lower]
        return [{
            "title": f"{make} {model} - Official Product Page",
            "url": base_url,
            "snippet": f"Official {make} product page where you can find specifications for {model}"
        }]
    
    return []


def search_with_duckduckgo(make: str, model: str, max_results: int = 10) -> List[Dict]:
    """
    Search for spec sheets using DuckDuckGo (no API key required).
    
    Args:
        make: Equipment manufacturer
        model: Equipment model
        max_results: Maximum number of results to return
        
    Returns:
        List of search result dictionaries
    """
    search_query = f'"{make}" "{model}" specification sheet manual datasheet'
    
    try:
        # DuckDuckGo instant answer API
        url = "https://api.duckduckgo.com/"
        params = {
            "q": search_query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1"
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        search_results = []
        
        # Extract related topics
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if "FirstURL" in topic and "Text" in topic:
                search_results.append({
                    "title": topic.get("Text", "").split(" - ")[0] if " - " in topic.get("Text", "") else topic.get("Text", "")[:100],
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", "")
                })
        
        logging.info(f"DuckDuckGo search returned {len(search_results)} results for {make} {model}")
        return search_results
        
    except Exception as e:
        logging.warning(f"DuckDuckGo search failed for {make} {model}: {e}")
        return []


def get_equipment_spec_urls(make: str, model: str, search_methods: List[str] = None) -> List[Dict]:
    """
    Try multiple search methods to find equipment specification URLs.
    
    Args:
        make: Equipment manufacturer
        model: Equipment model
        search_methods: List of search methods to try (default: all available)
        
    Returns:
        List of search result dictionaries
    """
    if search_methods is None:
        search_methods = ["manufacturer_direct", "duckduckgo"]
    
    all_results = []
    
    # Try manufacturer direct search first (most reliable)
    if "manufacturer_direct" in search_methods:
        manufacturer_results = search_manufacturer_direct(make, model)
        all_results.extend(manufacturer_results)
        if manufacturer_results:
            logging.info(f"Found manufacturer direct URLs for {make} {model}")
    
    # Try DuckDuckGo search
    if "duckduckgo" in search_methods:
        duckduckgo_results = search_with_duckduckgo(make, model)
        all_results.extend(duckduckgo_results)
        if duckduckgo_results:
            logging.info(f"Found DuckDuckGo results for {make} {model}")
    
    # Try Bing API if key is provided
    if "bing_api" in search_methods:
        # This would require setting up a Bing API key
        # bing_results = search_with_bing_api(make, model, BING_API_KEY)
        # all_results.extend(bing_results)
        pass
    
    return all_results


def demo_alternative_search():
    """Demonstrate alternative search methods."""
    
    print("Alternative Search Methods for Equipment Spec Sheets")
    print("=" * 55)
    
    test_equipment = [
        {"make": "Caterpillar", "model": "C15 Engine"},
        {"make": "GE", "model": "LM6000 Gas Turbine"},
        {"make": "Cleaver Brooks", "model": "CB Boiler"}
    ]
    
    for equipment in test_equipment:
        make = equipment["make"]
        model = equipment["model"]
        
        print(f"\nSearching for: {make} {model}")
        print("-" * 30)
        
        # Try manufacturer direct search
        manufacturer_results = search_manufacturer_direct(make, model)
        if manufacturer_results:
            print("Manufacturer Direct Results:")
            for result in manufacturer_results:
                print(f"  • {result['title']}")
                print(f"    URL: {result['url']}")
        
        # Try DuckDuckGo search
        duckduckgo_results = search_with_duckduckgo(make, model)
        if duckduckgo_results:
            print("DuckDuckGo Results:")
            for result in duckduckgo_results:
                print(f"  • {result['title']}")
                print(f"    URL: {result['url']}")
                if result['snippet']:
                    print(f"    Snippet: {result['snippet'][:100]}...")
        
        if not manufacturer_results and not duckduckgo_results:
            print("  No alternative search results found.")


if __name__ == "__main__":
    demo_alternative_search()
