import pandas as pd
import plotly.graph_objects as go

cov = pd.read_csv('/home/afhubbard/permit_data_extraction/data/processed/analysis/coverage_vs_echo_majors.csv')
cov['pct'] = cov['coverage'] * 100
national_m = cov['matched_facilities'].sum()
national_t = cov['echo_active_major_facilities'].sum()

DKGRAY = "#212121"; GRAY = "#8A8A8A"
scale = [[0.0, "#F2F6FD"], [0.25, "#B7D3F6"], [0.5, "#6DA7EC"],
         [0.75, "#2A78D6"], [1.0, "#0D366B"]]

fig = go.Figure(go.Choropleth(
    locations=cov['state'], locationmode='USA-states', z=cov['pct'],
    zmin=0, zmax=100, colorscale=scale,
    marker_line_color='white', marker_line_width=0.8,
    colorbar=dict(title=dict(text="Coverage", font=dict(size=13, color=DKGRAY)),
                  ticksuffix="%", thickness=14, len=0.75,
                  tickfont=dict(size=12, color=DKGRAY)),
    customdata=cov[['matched_facilities', 'echo_active_major_facilities']],
    hovertemplate="%{location}: %{z:.0f}%% (%{customdata[0]}/%{customdata[1]})<extra></extra>",
))

# callouts: big states + the zero-coverage holes
CALLOUTS = {
    "LA": (30.9, -92.3, "94%"), "TX": (31.2, -99.3, "66%"),
    "CA": (37.2, -119.5, "71%"), "IN": (39.9, -86.3, "90%"),
    "OH": (40.2, -82.8, "74%"), "PA": (40.9, -77.7, "80%"),
    "KS": (38.5, -98.4, "KS 0%"), "NE": (41.5, -99.7, "NE 0%"),
}
for st_, (lat, lon, label) in CALLOUTS.items():
    dark_bg = cov.loc[cov.state == st_, 'pct'].iloc[0] > 55
    fig.add_trace(go.Scattergeo(
        lat=[lat], lon=[lon], mode='text', text=[label],
        textfont=dict(size=13, color='white' if dark_bg else DKGRAY,
                      family='Arial'),
        showlegend=False, hoverinfo='skip'))

fig.update_geos(scope='usa', bgcolor='white', lakecolor='white',
                showlakes=True, subunitcolor='white')
fig.update_layout(
    width=1560, height=840, margin=dict(l=10, r=10, t=88, b=30),
    paper_bgcolor='white', font=dict(family='Arial'),
    title=dict(
        text=(f"<b>Share of EPA ECHO active major facilities in our database, by state</b>"
              f"<br><sup>National: {national_m:,} of {national_t:,} facilities matched "
              f"({national_m/national_t:.0%}) · white = no usable portal (KS, NE, DE, DC)"
              f" · PR (2%) not shown</sup>"),
        font=dict(size=26, color=DKGRAY), x=0.02, xanchor='left'),
)
fig.write_image('chart_echo_map.png', scale=2)
print("map written")
