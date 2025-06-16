import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

from permit_data_extraction.config import EXTERNAL_DATA_DIR

# Set the style for better visualization
# plt.style.use('seaborn')
# sns.set_palette("husl")

def create_naics_pie_chart():
    # Read the FRS facilities data
    data_path = Path(f"{EXTERNAL_DATA_DIR}/FRS_NAICS_CODES.csv")
    df = pd.read_csv(data_path)
    
    # Filter for valid 5 or 6-digit NAICS codes
    df['NAICS_CODE'] = df['NAICS_CODE'].astype(str).str.zfill(6)  # Pad with zeros to ensure 6 digits
    valid_naics = df[
        (df['NAICS_CODE'].str.len() >= 5) &  # Must be 5 or 6 digits
        (df['NAICS_CODE'] != '999999') &     # Exclude 999999
        (df['NAICS_CODE'].str.isdigit())     # Must be numeric
    ]
    
    # Extract first 2 digits and count facilities
    valid_naics['NAICS_GROUP'] = valid_naics['NAICS_CODE'].str[:2]
    naics_counts = valid_naics['NAICS_GROUP'].value_counts()
    
    # Get top 20 NAICS groups and group the rest as "Other"
    top_20_naics = naics_counts.head(20)
    other_count = naics_counts[20:].sum()
    
    # Create the final data for plotting
    plot_data = pd.concat([top_20_naics, pd.Series({'Other': other_count})])
    
    # Sort the data by NAICS code (except 'Other' which goes last)
    sorted_data = pd.concat([
        plot_data[plot_data.index != 'Other'].sort_index(),
        plot_data[plot_data.index == 'Other']
    ])
    
    # Calculate percentages
    total = sorted_data.sum()
    percentages = (sorted_data / total * 100).round(1)
    
    # Create labels with percentages
    labels = [f'{code}\n({pct}%)' for code, pct in zip(sorted_data.index, percentages)]
    
    # Create the pie chart
    plt.figure(figsize=(15, 10))
    plt.pie(sorted_data.values, labels=labels, autopct='')  # Remove default autopct since we added it to labels
    plt.title('Distribution of Facilities by NAICS Code Group (Top 20 + Other)\nFirst 2 digits of NAICS code (5-6 digit codes)')
    
    # Save the plot
    output_path = Path("reports/figures/naics_distribution.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, bbox_inches='tight', dpi=300)
    plt.close()

if __name__ == "__main__":
    create_naics_pie_chart() 