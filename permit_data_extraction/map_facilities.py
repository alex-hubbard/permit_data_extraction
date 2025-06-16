import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
from pathlib import Path
from permit_data_extraction.config import EXTERNAL_DATA_DIR

def create_facilities_map():
    # Read the FRS facilities data
    data_path = Path(f"{EXTERNAL_DATA_DIR}/FRS_FACILITIES.csv")
    df = pd.read_csv(data_path)
    
    # Read US states shapefile
    states = gpd.read_file(f"{EXTERNAL_DATA_DIR}/us_states/us_states.shp")
    
    # Filter out non-contiguous states
    contiguous_states = states[~states['STATE'].isin(['Alaska', 'Hawaii', 'Puerto Rico', 'Guam', 'American Samoa', 'United States Virgin Islands', 'Commonwealth of the Northern Mariana Islands'])]
    
    # Get the bounding box of contiguous states
    bounds = contiguous_states.total_bounds
    
    # Define non-contiguous state abbreviations
    non_contiguous = ['AK', 'HI', 'PR', 'GU', 'AS', 'VI', 'MP']
    
    # Filter for facilities with valid coordinates, in contiguous states, and within map bounds
    valid_coords = df[
        df['LATITUDE_MEASURE'].notna() & 
        df['LONGITUDE_MEASURE'].notna() &
        (df['LATITUDE_MEASURE'] != 0) & 
        (df['LONGITUDE_MEASURE'] != 0) &
        (~df['FAC_STATE'].isin(non_contiguous)) &
        (df['LONGITUDE_MEASURE'] >= bounds[0]) &  # min longitude
        (df['LONGITUDE_MEASURE'] <= bounds[2]) &  # max longitude
        (df['LATITUDE_MEASURE'] >= bounds[1]) &   # min latitude
        (df['LATITUDE_MEASURE'] <= bounds[3])     # max latitude
    ]
    
    # Create GeoDataFrame from coordinates
    gdf = gpd.GeoDataFrame(
        valid_coords,
        geometry=gpd.points_from_xy(
            valid_coords['LONGITUDE_MEASURE'],
            valid_coords['LATITUDE_MEASURE']
        ),
        crs="EPSG:4326"
    )
    
    # Filter out points that don't intersect with any state
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
    plt.title('Facility Locations in the Contiguous United States', fontsize=16)
    ax.set_axis_off()
    
    # Save the map
    output_path = Path("reports/figures/facilities_map.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()

if __name__ == "__main__":
    create_facilities_map() 