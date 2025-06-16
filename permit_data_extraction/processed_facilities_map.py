import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
from geopy.geocoders import GoogleV3
from tqdm import tqdm
from time import sleep
from dotenv import dotenv_values
from pathlib import Path
from permit_data_extraction.config import PROCESSED_DATA_DIR, EXTERNAL_DATA_DIR

# --- IMPORTANT ---
# Replace 'YOUR_GOOGLE_MAPS_API_KEY' with your actual Google Maps API key.
api_key = dotenv_values()['MAPS_API_KEY']

# Initialize the geolocator with GoogleV3
geolocator = GoogleV3(api_key=api_key)

# Load the dataset
try:
    df = pd.read_excel(PROCESSED_DATA_DIR / 'permit_data_extracted.xlsx')
except FileNotFoundError:
    print("Error: The file 'permit_data_extracted.xlsx' was not found.")
    exit()

# Prepare the address for geocoding
df['Full Address'] = df['Facility Address'] + ', ' + df['Facility City'] + ', ' + df['Facility State']

# Geocode the addresses to get latitude and longitude
tqdm.pandas(desc="Geocoding addresses with Google Maps")
df['Coordinates'] = df['Full Address'].progress_apply(
    lambda x: geolocator.geocode(x, timeout=10)
)
# A short delay can be helpful to avoid hitting rate limits too quickly.
sleep(0.1)

# Extract latitude and longitude from the geocoded results
df['Latitude'] = df['Coordinates'].apply(lambda x: x.latitude if x else None)
df['Longitude'] = df['Coordinates'].apply(lambda x: x.longitude if x else None)

# Drop rows where we couldn't get coordinates
df_map = df.dropna(subset=['Latitude', 'Longitude'])

if not df_map.empty:
    # Create GeoDataFrame from coordinates
    gdf = gpd.GeoDataFrame(
        df_map,
        geometry=gpd.points_from_xy(
            df_map['Longitude'],
            df_map['Latitude']
        ),
        crs="EPSG:4326"
    )
    
    # Read US states shapefile
    states = gpd.read_file(f"{EXTERNAL_DATA_DIR}/us_states/us_states.shp")
    
    # Filter out non-contiguous states
    contiguous_states = states[~states['STATE'].isin(['Alaska', 'Hawaii', 'Puerto Rico', 'Guam', 'American Samoa', 'United States Virgin Islands', 'Commonwealth of the Northern Mariana Islands'])]
    
    # Get the bounding box of contiguous states
    bounds = contiguous_states.total_bounds
    
    # Filter facilities to only those in contiguous states
    gdf = gdf[
        (gdf['Longitude'] >= bounds[0]) &
        (gdf['Longitude'] <= bounds[2]) &
        (gdf['Latitude'] >= bounds[1]) &
        (gdf['Latitude'] <= bounds[3])
    ]
    
    # Create a single polygon of all states
    states_union = contiguous_states.geometry.unary_union
    
    # Keep only points that intersect with the states
    gdf = gdf[gdf.geometry.intersects(states_union)]
    
    # Create the plot
    fig, ax = plt.subplots(figsize=(15, 10))
    
    # Plot states
    contiguous_states.boundary.plot(ax=ax, linewidth=0.5, color='gray')
    
    # Plot facilities
    gdf.plot(
        ax=ax,
        markersize=0.5,
        alpha=0.5,
        color='blue'
    )
    
    # Set the plot extent to match the state boundaries
    ax.set_xlim(bounds[0], bounds[2])
    ax.set_ylim(bounds[1], bounds[3])
    
    # Set title and remove axis
    plt.title('Permitted Facility Locations in the Contiguous United States', fontsize=16)
    ax.set_axis_off()
    
    # Save the map
    output_path = Path("reports/figures/processed_facilities_map.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    print(f"Map has been created and saved as '{output_path}'")
else:
    print("Could not geocode any of the addresses. The map cannot be created.")