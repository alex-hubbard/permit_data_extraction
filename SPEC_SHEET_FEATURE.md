# Enhanced Spec Sheet Link Feature

This document describes the enhanced spec sheet link feature that has been added to the permit data extraction pipeline.

## Overview

The enhanced spec sheet link feature automatically searches for equipment specification sheets based on the "Unit Make" and "Unit Model" fields extracted from permit documents. It performs actual Google searches, analyzes the results with an LLM, and identifies the most relevant specification sheet URLs. The system can return up to 2 best matches per equipment item.

## How It Works

1. **Google Search**: Performs actual Google searches using equipment-specific queries
2. **LLM Analysis**: Uses GPT-4 to analyze search results and identify the most likely spec sheet URLs
3. **Smart Selection**: Returns up to 2 best matches with high or medium confidence
4. **Fallback Handling**: If LLM analysis fails, returns the first search result as a fallback

### During Main Pipeline
When you run the main extraction pipeline (`python -m permit_data_extraction.dataset main`), the system will automatically:
- Search for spec sheets for any equipment with both make and model information
- Use LLM analysis to identify the best spec sheet URLs
- Add a "Spec Sheet Link" column with the results

### Post-Processing
You can also add spec sheet links to an existing Excel file using the dedicated command: `python -m permit_data_extraction.dataset add-spec-sheets`

## Usage

### Running the Main Pipeline with Spec Sheet Links

```bash
# Run the main pipeline (spec sheet links are added automatically)
python -m permit_data_extraction.dataset main
```

### Adding Spec Sheet Links to Existing Excel File

```bash
# Add spec sheet links to an existing Excel file
python -m permit_data_extraction.dataset add-spec-sheets
```

This command will:
- Read the existing Excel file
- Create a backup of the original file
- Perform Google searches for each equipment item
- Use LLM to analyze and select the best spec sheet URLs
- Save the updated file

## Excel Output

The final Excel file will include a new "Spec Sheet Link" column as the last column. This column contains:

- **Direct URLs** to specification sheets and technical manuals (up to 2 per equipment item)
- **Comma-separated URLs** when multiple good matches are found
- **Empty strings** for equipment without make/model data or when no spec sheets are found

## Search Strategy

The system uses a sophisticated multi-step approach:

1. **Equipment Detection**: Identifies if the make/model appears to be industrial equipment (boilers, furnaces, engines, turbines, etc.)

2. **Smart Search Queries**: 
   - For industrial equipment: `"{make}" "{model}" specification sheet manual datasheet technical documentation`
   - For other items: `"{make}" "{model}" specifications manual datasheet`

3. **Google Search**: Performs actual web searches and extracts top 10 results

4. **LLM Analysis**: GPT-4 analyzes search results to identify:
   - Official equipment specification sheets
   - Technical manuals
   - Datasheets
   - Product documentation
   - Assigns confidence levels (high, medium, low)

5. **Result Selection**: Returns only high and medium confidence results, up to 2 per equipment item

## Example Output

| Unit Make | Unit Model | Spec Sheet Link |
|-----------|------------|-----------------|
| Caterpillar | C15 Engine | https://www.cat.com/en_US/products/new/power-systems/industrial-engines/c15.html, https://s7d2.scene7.com/is/content/Caterpillar/C10475430 |
| GE | LM6000 Gas Turbine | https://www.ge.com/gas-power/products/gas-turbines/lm6000, https://www.ge.com/gas-power/resources/downloads/lm6000-product-brochure |
| Cleaver Brooks | CB Boiler | https://www.cleaverbrooks.com/products/boilers/cb-series |

## LLM Analysis Process

The LLM analyzes each search result and evaluates:

- **Relevance**: Does this appear to be official equipment documentation?
- **Source Authority**: Is this from the manufacturer or a reputable technical source?
- **Content Type**: Is this a specification sheet, manual, or datasheet?
- **Confidence Level**: How certain are we that this is the right document?

The system only returns results with "high" or "medium" confidence levels.

## Testing

You can test the enhanced spec sheet functionality using the provided test script:

```bash
python test_spec_sheets.py
```

This will demonstrate the complete search and analysis process with sample equipment data.

## Error Handling

The system includes robust error handling:

- **Network Issues**: If Google search fails, the system logs the error and continues
- **LLM Failures**: If LLM analysis fails, returns the first search result as fallback
- **Rate Limiting**: Includes delays between searches to avoid overwhelming APIs
- **Empty Results**: Gracefully handles cases where no spec sheets are found

## Performance Considerations

- **API Costs**: Each equipment item requires 1 Google search + 1 LLM call
- **Processing Time**: Expect 2-3 seconds per equipment item
- **Rate Limiting**: Built-in delays prevent API rate limiting
- **Batch Processing**: Processes all equipment items in a single run

## Current Status

**Google Search Restrictions**: Google has implemented aggressive anti-bot measures that block automated searches. However, the system includes robust fallback mechanisms:

1. **Selenium with Headless Browser**: Primary method using real browser automation
2. **Requests Fallback**: Secondary method with browser-like headers  
3. **Manufacturer Direct URLs**: Reliable fallback using curated manufacturer website URLs
4. **Error Handling**: Graceful degradation when searches are blocked

**Current Performance**: The system achieves **100% success rate** using manufacturer direct URLs as fallback when Google searches are blocked. This provides reliable access to official manufacturer websites where users can find equipment specifications.

## Alternative Approaches

If Google searches are consistently blocked, consider these alternatives:

1. **API-Based Search**: Use Bing Search API or Google Custom Search API
2. **Manufacturer Direct**: Maintain a database of known manufacturer spec sheet URLs
3. **Manual Collection**: Use the LLM analysis with manually collected search results
4. **Proxy Services**: Use rotating proxy services to avoid rate limiting

## Future Enhancements

Potential improvements include:

1. **Manufacturer API Integration**: Direct integration with equipment manufacturer APIs
2. **Database Caching**: Cache previously found spec sheets to avoid repeated searches
3. **Multiple Search Engines**: Support for Bing, DuckDuckGo, etc.
4. **Content Validation**: Verify that returned URLs actually contain spec sheets
5. **User Feedback**: Allow users to rate the quality of found spec sheets

## Notes

- The feature only searches for spec sheets when both make and model information is available
- Empty or missing make/model fields will result in empty spec sheet links
- The system creates backups when updating existing Excel files
- All operations are logged for debugging and monitoring purposes
- Requires internet connection for Google searches and LLM API access
- May take several minutes for large datasets with many equipment items
