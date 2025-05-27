import os
import re
import pandas as pd
from pathlib import Path
from collections import Counter
from typing import List, Dict, Set
import json
from loguru import logger
import google.generativeai as genai
from dotenv import load_dotenv, dotenv_values
import time
from tenacity import retry, stop_after_attempt, wait_exponential

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR, PROCESSED_DATA_DIR

# Load environment variables
load_dotenv()
API_KEY = dotenv_values()['API_KEY']

# Configure Gemini API
genai.configure(api_key=API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Rate limiting settings
RATE_LIMIT_DELAY = 60  # seconds to wait between batches
BATCH_SIZE = 5  # number of files to process in each batch

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_fields_with_gemini(text: str) -> Dict[str, List[str]]:
    """
    Use Gemini to extract potential fields and their values from text.
    Includes retry logic for API failures.
    
    Args:
        text (str): The text to analyze
        
    Returns:
        Dict[str, List[str]]: Dictionary of field names and their potential values
    """
    prompt = """
    Analyze this permit text and identify all possible data fields and their values.
    Return the results as a JSON object where:
    - Keys are the field names (e.g., "permit_number", "applicant_name", "project_cost")
    - Values are lists of the values found for each field
    
    Only include fields that have clear values in the text.
    Format the response as a valid JSON object.
    
    Text to analyze:
    {text}
    """
    
    try:
        response = model.generate_content(prompt.format(text=text))
        # Extract JSON from the response
        json_str = response.text
        # Find the JSON object in the response
        json_match = re.search(r'\{.*\}', json_str, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return {}
    except Exception as e:
        logger.error(f"Error extracting fields with Gemini: {str(e)}")
        raise  # Re-raise for retry logic

def process_batch(text_files: List[Path], start_idx: int) -> List[Dict]:
    """
    Process a batch of files with rate limiting.
    
    Args:
        text_files (List[Path]): List of all text files
        start_idx (int): Starting index for this batch
        
    Returns:
        List[Dict]: List of results for processed files
    """
    batch_results = []
    end_idx = min(start_idx + BATCH_SIZE, len(text_files))
    
    for i in range(start_idx, end_idx):
        file_path = text_files[i]
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
            
            # Extract potential fields using Gemini
            fields = extract_fields_with_gemini(text)
            
            # Add to results
            batch_results.append({
                'file_name': file_path.name,
                'fields_found': list(fields.keys()),
                'field_values': fields
            })
            
            logger.info(f"Processed {file_path.name}")
            
        except Exception as e:
            logger.error(f"Error processing {file_path}: {str(e)}")
            batch_results.append({
                'file_name': file_path.name,
                'fields_found': [],
                'field_values': {}
            })
    
    return batch_results

def analyze_permit_files() -> pd.DataFrame:
    """
    Analyze all permit text files and create a summary of available data points.
    Includes rate limiting to handle TPM limits.
    
    Returns:
        pd.DataFrame: DataFrame containing the analysis results
    """
    # Get all text files
    text_files = list(Path(f'{INTERIM_DATA_DIR}/extracted_text').rglob('*.txt'))
    logger.info(f"Found {len(text_files)} text files to analyze")
    
    # Store results
    all_fields = []
    field_frequency = Counter()
    
    # Process files in batches
    for start_idx in range(0, len(text_files), BATCH_SIZE):
        logger.info(f"Processing batch starting at index {start_idx}")
        
        # Process current batch
        batch_results = process_batch(text_files, start_idx)
        all_fields.extend(batch_results)
        
        # Update field frequency
        for result in batch_results:
            field_frequency.update(result['fields_found'])
        
        # Wait before processing next batch
        if start_idx + BATCH_SIZE < len(text_files):
            logger.info(f"Waiting {RATE_LIMIT_DELAY} seconds before next batch...")
            time.sleep(RATE_LIMIT_DELAY)
    
    # Create summary DataFrame
    summary = pd.DataFrame(all_fields)
    
    # Add field frequency information
    field_freq_df = pd.DataFrame({
        'field_name': list(field_frequency.keys()),
        'frequency': list(field_frequency.values()),
        'percentage': [freq/len(text_files)*100 for freq in field_frequency.values()]
    }).sort_values('frequency', ascending=False)
    
    # Save results
    output_dir = PROCESSED_DATA_DIR / 'analysis'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save detailed results
    summary.to_csv(output_dir / 'permit_field_analysis.csv', index=False)
    field_freq_df.to_csv(output_dir / 'field_frequency.csv', index=False)
    
    # Save field values for reference
    with open(output_dir / 'field_values.json', 'w') as f:
        json.dump(all_fields, f, indent=2)
    
    logger.info(f"Analysis complete. Results saved to {output_dir}")
    return field_freq_df

if __name__ == "__main__":
    # Run the analysis
    field_frequency = analyze_permit_files()
    
    # Print summary
    print("\nField Frequency Summary:")
    print("=======================")
    print(field_frequency.to_string(index=False)) 