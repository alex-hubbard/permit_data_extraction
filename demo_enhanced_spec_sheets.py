#!/usr/bin/env python3
"""
Demonstration script for the enhanced spec sheet link functionality.
This script shows how the system would work with mock search results.
"""

import json
import sys
import os

# Add the permit_data_extraction module to the path
sys.path.append(os.path.join(os.path.dirname(__file__), 'permit_data_extraction'))

from permit_data_extraction.dataset import analyze_search_results_with_llm, configure_llm

def create_mock_search_results():
    """Create mock search results to demonstrate the LLM analysis."""
    return [
        {
            "title": "Caterpillar C15 Engine Specifications - Official Documentation",
            "url": "https://www.cat.com/en_US/products/new/power-systems/industrial-engines/c15.html",
            "snippet": "Official Caterpillar C15 engine specifications, technical data, performance metrics, and installation guidelines from the manufacturer."
        },
        {
            "title": "C15 Engine Datasheet PDF Download",
            "url": "https://s7d2.scene7.com/is/content/Caterpillar/C10475430",
            "snippet": "Download the complete Caterpillar C15 engine datasheet in PDF format with detailed technical specifications."
        },
        {
            "title": "Used C15 Engines for Sale - Equipment Trader",
            "url": "https://www.equipmenttrader.com/caterpillar-c15-engines",
            "snippet": "Browse used Caterpillar C15 engines for sale. Find great deals on industrial engines with various specifications."
        },
        {
            "title": "C15 Engine Maintenance Guide - Fleet Management",
            "url": "https://www.fleetmanagement.com/c15-maintenance-guide",
            "snippet": "Comprehensive maintenance guide for Caterpillar C15 engines including service intervals and troubleshooting tips."
        },
        {
            "title": "Caterpillar C15 Engine Parts Catalog",
            "url": "https://parts.cat.com/c15-engine-parts",
            "snippet": "Browse and purchase genuine Caterpillar C15 engine parts and components from the official parts catalog."
        }
    ]

def demonstrate_llm_analysis():
    """Demonstrate how the LLM analyzes search results to identify spec sheets."""
    
    print("Enhanced Spec Sheet Link Feature - LLM Analysis Demonstration")
    print("=" * 65)
    
    # Configure LLM client
    print("1. Configuring LLM client...")
    llm_client = configure_llm()
    if not llm_client:
        print("ERROR: Could not configure LLM client.")
        return
    
    # Create mock search results
    print("\n2. Creating mock search results...")
    mock_results = create_mock_search_results()
    
    print(f"Mock search results for 'Caterpillar C15 Engine':")
    for i, result in enumerate(mock_results, 1):
        print(f"\n{i}. {result['title']}")
        print(f"   URL: {result['url']}")
        print(f"   Snippet: {result['snippet']}")
    
    # Analyze with LLM
    print("\n3. Analyzing search results with LLM...")
    print("The LLM will now analyze these results and identify which ones are most likely to be specification sheets...")
    
    spec_sheets = analyze_search_results_with_llm("Caterpillar", "C15 Engine", mock_results, llm_client)
    
    print("\n4. LLM Analysis Results:")
    print("-" * 30)
    
    if spec_sheets:
        print(f"LLM identified {len(spec_sheets)} specification sheet(s):")
        for i, sheet in enumerate(spec_sheets, 1):
            print(f"\n{i}. {sheet['title']}")
            print(f"   URL: {sheet['url']}")
            print(f"   Confidence: {sheet['confidence']}")
            print(f"   Reason: {sheet['reason']}")
        
        # Show final result
        urls = [sheet['url'] for sheet in spec_sheets[:2]]
        final_result = ", ".join(urls)
        print(f"\nFinal Spec Sheet Links: {final_result}")
    else:
        print("LLM did not identify any high or medium confidence specification sheets.")
    
    print("\n" + "=" * 65)
    print("Demonstration completed!")
    print("\nIn the actual pipeline, this process would:")
    print("1. Perform real Google searches for each equipment item")
    print("2. Use LLM analysis to identify the best spec sheet URLs")
    print("3. Return up to 2 best matches per equipment item")
    print("4. Add the results to the Excel output as comma-separated URLs")

if __name__ == "__main__":
    demonstrate_llm_analysis()
