#!/usr/bin/env python3
"""Roll the free-text ``Unit Type`` field up into a two-tier equipment taxonomy
and flag each row as a combustion or non-combustion source.

The extraction prompt asked the LLM for a free-text unit type, so the release
carries ~62,000 distinct normalised strings ("boiler", "boiler/process heater",
"stationary rice (ci)", "defr", ...).  This module maps every row to

  * ``Equipment Subcategory``  ~70 fine categories, aligned where possible with
        the EPA Emissions Inventory System (EIS) unit-type code list (Boiler 100,
        Turbine 120, RICE 160, Process Heater 180, Storage Tank 400, Conveyor
        760, ...); the EIS code is kept in the taxonomy table, not in the data
  * ``Equipment Category``     16 coarse categories plus "Unspecified / generic"
        (boilers and process heaters are deliberately separate: different EIS
        codes, different standards, different decarbonisation pathways)
  * ``Combustion Source``      combustion / non-combustion / unknown
  * ``Equipment Category Basis`` which field decided it: unit_type,
        unit_description, capacity_unit, or none

Method: ordered regex rules, first match wins, most specific first.  STRONG
rules (a named piece of equipment) run on the normalised Unit Type (first
';'-separated value), then on Unit Description.  WEAK rules (generic words:
"process", "vent", "combustion", "storage", "loading") run afterwards in the
same order, so a generic Unit Type like "combustion unit" is decided by a
specific description ("heaters/boilers") before it falls back to the generic
bucket.  Rows still unmatched take a Capacity Unit hint (hp -> engine,
MMBtu/hr -> fuel-burning equipment, gallons/bbl -> tank, tons/hr -> material
handling).  Engine duty (emergency / fire pump / compressor drive /
non-emergency / unspecified) and the heater split (process vs comfort) are
decided from Unit Type and Unit Description together, reusing the regexes from
scripts/build_dc_generation_sites.py so the two tables agree.

Combustion flag: each fine category has a default.  Row-level overrides:
fuel-fired-by-design categories flip to non-combustion when the Fuel Type or
unit text says electric / steam-heated and no burnable fuel is named; generic
and "other process equipment" categories flip to combustion when a burnable
fuel is named or the unit text says fired / burner / combustion; rows with no
Unit Type, no Unit Description and no fuel are "unknown".

Usage (from the repo root):
    python scripts/classify_unit_types.py \
        --parquet data/processed/dashboard/permits_v5f.parquet \
        --out-dir data/processed/analysis/unit_type_taxonomy

Outputs (in --out-dir):
    unit_type_categories_rows.parquet   per-row columns, same row order as input
    unit_type_string_map.csv            distinct normalised Unit Type ->
                                        category, with row counts (review this)
    taxonomy.csv                        fine -> coarse, EIS code, default flag
    coverage_coarse.csv, coverage_fine.csv, combustion_by_coarse.csv
    unmapped_top.csv                    most frequent strings still unassigned
    samples_by_fine.csv                 15 random rows per fine category

``classify_frame`` is importable; scripts/add_equipment_columns.py uses it to
append the four columns to the release CSV and parquet.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

OUT_COLS = ["Equipment Category", "Equipment Subcategory", "Combustion Source", "Equipment Category Basis"]

# --------------------------------------------------------------------------- #
# Taxonomy: fine -> (coarse, EIS unit-type code, default combustion flag)
# --------------------------------------------------------------------------- #
C, N = "combustion", "non-combustion"
TAXONOMY: dict[str, tuple[str, int | None, str]] = {
    # boilers (steam / hot water, indirect) -- the MACT DDDDD "boiler or process
    # heater" wording rolls up here because that population is mostly boilers
    "Boiler":                                   ("Boilers", 100, C),
    "Boiler or process heater (either)":        ("Boilers", None, C),
    # process heaters (direct-fired heating of a process stream)
    "Process heater":                           ("Process heaters", 180, C),
    # comfort heating and fuel-burning equipment the permit does not name
    "Space & water heating (comfort)":          ("Comfort heating & other fuel-burning equipment", None, C),
    "Fuel-burning equipment, unspecified":      ("Comfort heating & other fuel-burning equipment", 290, C),
    # engines
    "Emergency / standby generator engine":     ("Reciprocating engines", 160, C),
    "Fire pump engine":                         ("Reciprocating engines", 160, C),
    "Compressor / pump drive engine":           ("Reciprocating engines", 160, C),
    "Non-emergency / prime engine":             ("Reciprocating engines", 160, C),
    "Engine, duty unspecified":                 ("Reciprocating engines", 160, C),
    "Engine test cell":                         ("Reciprocating engines", 170, C),
    # turbines & other power
    "Combustion turbine (simple cycle / unspecified)": ("Turbines & other power generation", 120, C),
    "Combined cycle turbine & HRSG":            ("Turbines & other power generation", 140, C),
    "Duct burner":                              ("Turbines & other power generation", 150, C),
    "Fuel cell, steam turbine & other non-combustion power": ("Turbines & other power generation", None, N),
    # process heating
    "Furnace (metal melting, heat treat, glass)": ("Furnaces, ovens, dryers & kilns", 200, C),
    "Oven (curing, drying, baking)":            ("Furnaces, ovens, dryers & kilns", None, C),
    "Dryer":                                    ("Furnaces, ovens, dryers & kilns", 255, C),
    "Kiln & calciner":                          ("Furnaces, ovens, dryers & kilns", 210, C),
    "Cooker, fryer, roaster & smokehouse":      ("Furnaces, ovens, dryers & kilns", None, C),
    # destruction
    "Flare & vapor combustor":                  ("Flares, incinerators & oxidizers", 280, C),
    "Incinerator & waste combustor":            ("Flares, incinerators & oxidizers", 270, C),
    "Thermal / catalytic oxidizer":             ("Flares, incinerators & oxidizers", 610, C),
    # tanks
    "Storage tank":                             ("Storage tanks & vessels", 400, N),
    "Fermentation & wine tank":                 ("Storage tanks & vessels", None, N),
    "Process tank & open vat":                  ("Storage tanks & vessels", 410, N),
    "Liquid loading rack & fuel dispensing":    ("Storage tanks & vessels", 480, N),
    # bulk material
    "Conveyor & transfer point":                ("Material handling & mineral processing", 760, N),
    "Crusher, grinder, mill & screen":          ("Material handling & mineral processing", 720, N),
    "Silo, bin & hopper":                       ("Material handling & mineral processing", 780, N),
    "Storage pile & material storage":          ("Material handling & mineral processing", 785, N),
    "Bulk loading, unloading & loadout":        ("Material handling & mineral processing", 790, N),
    "Aggregate, concrete & asphalt plant":      ("Material handling & mineral processing", 790, N),
    "Material handling, unspecified":           ("Material handling & mineral processing", 790, N),
    # coating & solvent
    "Spray booth & coating line":               ("Surface coating, printing & solvent use", 450, N),
    "Printing line":                            ("Surface coating, printing & solvent use", 470, N),
    "Degreaser & solvent cleaning":             ("Surface coating, printing & solvent use", 430, N),
    "Adhesive, resin & fiberglass application": ("Surface coating, printing & solvent use", None, N),
    "Plating, pickling & surface treatment":    ("Surface coating, printing & solvent use", None, N),
    # chemical / petroleum process
    "Reactor":                                  ("Chemical & petroleum process equipment", 600, N),
    "Distillation column, stripper & absorber": ("Chemical & petroleum process equipment", 620, N),
    "Separator, process vessel & knockout":     ("Chemical & petroleum process equipment", None, N),
    "Glycol dehydrator, amine & gas treating":  ("Chemical & petroleum process equipment", None, N),
    "Mixer, blender & kettle":                  ("Chemical & petroleum process equipment", 640, N),
    "Evaporator, centrifuge, filter & other process equipment": ("Chemical & petroleum process equipment", 690, N),
    "Vapor recovery unit":                      ("Chemical & petroleum process equipment", None, N),
    # fabrication
    "Welding & thermal cutting":                ("Fabrication & forming (metal, wood, plastics)", None, N),
    "Metal machining, grinding & forming":      ("Fabrication & forming (metal, wood, plastics)", None, N),
    "Abrasive blasting":                        ("Fabrication & forming (metal, wood, plastics)", None, N),
    "Woodworking":                              ("Fabrication & forming (metal, wood, plastics)", None, N),
    "Foundry & casting":                        ("Fabrication & forming (metal, wood, plastics)", None, N),
    "Plastics extrusion & molding":             ("Fabrication & forming (metal, wood, plastics)", None, N),
    # fugitive & area
    "Equipment leaks & fugitive components":    ("Fugitive & area sources", 350, N),
    "Roads & open fugitive dust":               ("Fugitive & area sources", 300, N),
    "Wastewater, landfill & ponds":             ("Fugitive & area sources", 490, N),
    "Facility-wide / area source":              ("Fugitive & area sources", None, N),
    # control
    "Baghouse & fabric filter":                 ("Air pollution control devices", None, N),
    "Scrubber":                                 ("Air pollution control devices", None, N),
    "Cyclone & mechanical collector":           ("Air pollution control devices", None, N),
    "Carbon adsorber":                          ("Air pollution control devices", None, N),
    "ESP, SCR & other control device":          ("Air pollution control devices", None, N),
    # utilities
    "Cooling tower":                            ("Cooling, compression & fluid handling", 680, N),
    "Compressor, blower, fan & vacuum":         ("Cooling, compression & fluid handling", None, N),
    "Pump":                                     ("Cooling, compression & fluid handling", None, N),
    "Heat exchanger, condenser & refrigeration": ("Cooling, compression & fluid handling", None, N),
    # generic
    "Generic process or production line":       ("Unspecified / generic", 690, N),
    "Emission point, stack or vent":            ("Unspecified / generic", 310, N),
    "Laboratory, maintenance & misc activity":  ("Unspecified / generic", 999, N),
    "Unspecified":                              ("Unspecified / generic", 999, N),
}
FINE_TO_COARSE = {k: v[0] for k, v in TAXONOMY.items()}
FINE_TO_EIS = {k: v[1] for k, v in TAXONOMY.items()}
FINE_TO_FLAG = {k: v[2] for k, v in TAXONOMY.items()}

# Fuel-fired categories that are sometimes electric / steam heated.
ELECTRIC_OVERRIDABLE = {
    "Boiler", "Process heater", "Boiler or process heater (either)", "Space & water heating (comfort)",
    "Fuel-burning equipment, unspecified", "Furnace (metal melting, heat treat, glass)",
    "Oven (curing, drying, baking)", "Dryer", "Kiln & calciner", "Cooker, fryer, roaster & smokehouse",
    "Thermal / catalytic oxidizer",
}
# Non-combustion-by-default categories that flip to combustion on evidence.
# Generic buckets accept a named burnable fuel alone; process-equipment buckets
# need "fired"/"burner"/"combustion" in the unit text or a heat-input capacity
# unit, because their Fuel Type column often names the material handled
# ("coal" for a coal conveyor, "propane" for a propane vaporizer).
FUEL_OVERRIDABLE_GENERIC = {
    "Generic process or production line", "Emission point, stack or vent",
    "Laboratory, maintenance & misc activity", "Unspecified", "Facility-wide / area source",
}
FUEL_OVERRIDABLE_FIRED_ONLY = {
    "Material handling, unspecified", "Evaporator, centrifuge, filter & other process equipment",
    "Separator, process vessel & knockout", "Reactor", "Glycol dehydrator, amine & gas treating",
    "Mixer, blender & kettle", "Heat exchanger, condenser & refrigeration",
}

# Regexes shared with scripts/build_dc_generation_sites.py (verbatim, so the
# engine duty split agrees with the data-center site table).
EMERG_RX = re.compile(r"(?<!non-)(?<!non )emergenc|standby|stand-by|backup|back-up|black ?start", re.I)
FIRE_RX = re.compile(r"fire ?pump|firewater|fire water", re.I)
PRIME_RX = re.compile(r"non-?\s?emergency|prime power|prime[- ]rated|continuous|behind[- ]the[- ]meter|base ?load|microgrid|power plant|generation plant|generating plant|peaking|demand response|load (?:shed|shave)|grid support|export", re.I)
TURB_RX = re.compile(r"turbine|\bctg?-?\d|combustion turb|combined[- ]cycle|simple[- ]cycle|\bsgt[- ]?\d|\blm\d{4}|\b7ha\b|"
                     r"\bge (?:7f|7e|6b|7ha)|frame (?:6|7|9)|m501|aeroderiv|\bgtg?-?\d|titan \d|taurus \d|mars \d|centaur \d|"
                     r"tm2500|lms100|trent 60|\bft8\b|swiftpac", re.I)

# Placeholders resolved after matching (need Unit Description / Capacity Unit too).
ENGINE_, HEATER_, GENERATOR_, LOADING_, STORAGE_ = "__ENGINE__", "__HEATER__", "__GENERATOR__", "__LOADING__", "__STORAGE__"

# ---- STRONG rules: a named piece of equipment.  First match wins. ----------
STRONG: list[tuple[str, str]] = [
    ("Engine test cell",        r"test cell|test stand|dynamometer|engine test"),
    ("Fire pump engine",        r"fire ?pump|fire ?water pump|firewater"),
    ("Fuel cell, steam turbine & other non-combustion power", r"fuel cell|steam turbine|\bstg\b|wind turbine|solar (?:panel|pv|array|farm)|photovoltaic"),
    ("Combined cycle turbine & HRSG", r"combined[- ]cycle|\bhrsg\b|heat recovery steam gen"),
    ("Combustion turbine (simple cycle / unspecified)", r"turbine|\bctg\b|\bgtg\b|microturbine"),
    (ENGINE_,                   r"cogen|combined heat and power|\bchp\b"),
    ("Duct burner",             r"duct burner"),
    ("Boiler or process heater (either)", r"boiler\s*(?:/|or|;|and|,)\s*(?:process )?heater|heater\s*(?:/|or|;)\s*boiler"),
    ("Boiler",                  r"(?<!re)boiler|steam generat|dutch oven|pile burner|stoker|\begu\b|electric utility steam|recovery furnace|hot water generator"),
    (ENGINE_,                   r"\brice\b|reciprocating (?:internal|ic\b|engine|combustion|diesel|gas|natural)|internal combustion|"
                                r"\bic engine|\bice\b|\bci engine|\bsi engine|compression[- ]ignition|spark[- ]ignition|diesel engine|"
                                r"gas engine|natural gas engine|\bengine|genset|gen-set|generator set|\bsric\b|\b[24]s[lr]b\b|\bci ice|\bsi ice|"
                                r"emergency (?:diesel )?generator|standby generator|backup generator|diesel generator|"
                                r"electric(?:al|ity)? generator|auxiliary generator|emergency generator|diesel pump|"
                                r"emergency (?:diesel |water |flood control |sump )?pump|pump driver|light plant|^gen[- ]?\d"),
    ("Cooker, fryer, roaster & smokehouse", r"smoke generator|smokehouse|smoke house|smokhouse"),
    (GENERATOR_,                r"\bgenerator"),
    # heaters
    ("Process heater",          r"heater treater|line heater|hot oil heater|reboiler|process heater|cracking (?:heater|furnace)|reformer|"
                                r"fired heater|indirect heat|salt bath|heater\s*/\s*furnace|furnace\s*/\s*heater|fuel heater|crude heater|"
                                r"regen(?:eration)? (?:gas )?heater|submerged combustion vaporizer|thermal fluid heater|heater/burner|"
                                r"charge heater|feed heater|preheater|superheater|\bhot blast"),
    ("Space & water heating (comfort)",
                                r"space heat|unit heater|water heater|comfort heat|room heater|radiant|infrared|tube heater|"
                                r"make-?\s?up air|makeup air|air make|air handl|\bhvac\b|rooftop|roof top|\brtu\b|forced air furnace|"
                                r"air condition|facility heater|building heat|warehouse heater|ceiling heater|wall heater|air heater|"
                                r"furnace \(hvac\)|heat pump"),
    (HEATER_,                   r"heater|heating unit|indirect heating"),
    # process heating
    ("Kiln & calciner",         r"calcin|lime kiln|rotary kiln|cement kiln|char(?:coal)? kiln|kiln(?! dry)|clinker|coke oven"),
    ("Dryer",                   r"dry kiln|drying kiln|lumber kiln|veneer dryer|dryer|drier|drying (?!oven)|dehumidif"),
    ("Separator, process vessel & knockout", r"smelt dissolv"),
    ("Furnace (metal melting, heat treat, glass)",
                                r"furnace|forge|melter|melting|holding (?:furnace|pot)|crucible|cupola|smelt|heat treat|anneal|galvaniz|"
                                r"hot dip|induction heat|ladle|tundish|reheat|forehearth|\blehr\b"),
    ("Cooker, fryer, roaster & smokehouse", r"cooker|fryer|roaster|roasting|proof box|proofer|kettle cook|brew kettle|steam peel|griddle|\bgrill\b"),
    ("Oven (curing, drying, baking)", r"\boven|curing|cure |bake|baking|autoclave"),
    # destruction devices
    ("Incinerator & waste combustor", r"incinerat|cremat|air curtain|waste combust|open burn|burn pit|pathological|pyroly|detonation"),
    ("Flare & vapor combustor", r"flare|vapor combust|vapou?r combust|enclosed combust|(?<!waste )combustor|ground flare|thermal combustor"),
    ("Thermal / catalytic oxidizer", r"oxidi[sz]er|\brto\b|\brco\b|\bcto\b|afterburner|after burner"),
    # cooling tower before the chemical 'tower' rule
    ("Cooling tower",           r"cooling tower|cooling water tower|cooling cell|evaporative cool|cooling module|mechanical draft"),
    # control devices
    ("Baghouse & fabric filter", r"baghouse|bag house|dust collect|fabric filter|fabric collector|bag filter|cartridge filter|bin vent|"
                                r"filter receiver|silo vent|pulse jet|^filters?$|hepa|dust filter"),
    ("Scrubber",                r"scrubber|wet collector|venturi|packed bed|packed tower|caustic scrub|fume scrub"),
    ("Cyclone & mechanical collector", r"cyclone|rotoclone|multiclone|mechanical collector|^collector$|settling chamber|knockout box"),
    ("Carbon adsorber",         r"carbon ads|carbon bed|activated carbon|carbon canister|carbon drum|\badsorber|adsorption"),
    ("ESP, SCR & other control device",
                                r"electrostatic|\besp\b|\bscr\b|\bsncr\b|selective catalytic|catalytic reduc|catalyst|mist eliminator|demister|"
                                r"control device|control equipment|pollution control|emission control|vent gas control|\bcntl\b|^controls?$|"
                                r"vapor control|flare gas recovery|oxidation catalyst|dry sorbent|\bfgd\b|desulfur|spray tower|wet esp"),
    # surface treatment before tanks ("plating tank")
    ("Plating, pickling & surface treatment", r"plating|electroplat|pickl|anodiz|chromat|phosphat|passivat|\betch|electropolish|"
                                r"conversion coat|chrome tank|acid tank|acid bath|surface treat|galvanic|corona"),
    # process vessels before tanks ("knockout drum")
    ("Separator, process vessel & knockout", r"knock ?out|\bk\.?o\.? (?:drum|pot)|flash (?:drum|tank|vessel)|surge (?:drum|vessel)|reflux drum|"
                                r"blowcase|slug catcher|separator|accumulator|seal pot|blowdown|blow down|gun barrel|free water|treater|"
                                r"decanter|settler|skimmer|coalesc|degas|hotwell|hot well|splitter|ion exchang|purification bed"),
    # storage & liquids
    ("Fermentation & wine tank", r"ferment|wine|\bbrew|brite tank|aging tank|barrel (?:aging|storage)|cellar|lauter|wort|trub"),
    ("Liquid loading rack & fuel dispensing",
                                r"loading rack|loading arm|marine load|truck loading rack|tank truck|tank car|rail car loading|fuel dispens|"
                                r"gasoline dispens|dispensing|dispenser|\bgdf\b|fuel station|fueling|refuel|"
                                r"loading (?:of )?(?:gasoline|crude|condensate|fuel|oil|product)|liquid loading|transfer rack|unloading rack"),
    ("Process tank & open vat", r"process tank|mix(?:ing)? tank|blend(?:ing)? tank|dip tank|wash tank|cleaning tank|feed tank|open tank|"
                                r"open vat|\bvat\b|tank, open|surge tank|holding tank|day tank|batch tank|reaction tank|sump tank|"
                                r"quench tank|rinse tank|process vessel"),
    ("Storage tank",            r"tank|storage vessel|\bvessel|\bdefr\b|\bifr\b|\befr\b|\bast\b|\bust\b|fixed roof|floating roof|cone roof|"
                                r"dome roof|pressurized storage|storage sphere|\bdrum\b|tote|container|cargo tank|"
                                r"storage of (?:gasoline|fuel|oil|crude|solvent|chemical)|petroleum storage|bulk plant|tank farm"),
    # bulk material
    ("Aggregate, concrete & asphalt plant", r"concrete batch|batch plant|asphalt plant|asphalt production|hot mix|ready[- ]mix|aggregate plant|"
                                r"rock plant|sand and gravel plant|quarry"),
    ("Silo, bin & hopper",      r"silo|\bbins?\b|hopper|storage bin|day bin|surge bin|weigh bin|feed bin|product bin|cement storage|"
                                r"flour storage|grain storage|grain bin|grain tank|\bchest\b"),
    ("Storage pile & material storage", r"stockpile|storage pile|\bpiles?\b|open storage|material storage|outdoor storage|coal storage|"
                                r"storage yard|storage area|storage building|laydown|coal pile"),
    ("Conveyor & transfer point", r"conveyor|conveying|conveyance|transfer point|transfer tower|bucket elevator|\belevator|\bleg\b|airslide|"
                                r"air slide|screw feeder|belt feeder|\bfeeder|\bauger|\bchute|shute|pneumatic convey|grain handling|"
                                r"grain receiving|grain leg|receiving pit|truck dump|rail dump|\bdump pit|material transfer|transfer system|"
                                r"transfer operation|transfer station|^transfer$|weigh belt|drag chain|stacker|reclaimer"),
    # metal forming / specific fabrication before the size-reduction rule
    ("Metal machining, grinding & forming", r"rolling mill|wire draw|cold form|stamping|grinding booth|grinding/machining|wet machining|"
                                r"burr bench|deburr|\blaser\b|laser cut"),
    ("Evaporator, centrifuge, filter & other process equipment", r"filter press|paper machine|tissue machine|pulp|corrugat"),
    ("Crusher, grinder, mill & screen", r"crush|grind|\bmill\b|\bmilling\b|hammer ?mill|ball mill|pulveriz|shredder|screen(?!.?print)|sizing|"
                                r"classifier|granulat|chipper|\bhog\b|hogger|tub grinder|roll(?:er)? mill|raw mill|finish mill|coal mill|"
                                r"sifter|sizer|screener|debarker|flaker|flaking"),
    ("Bulk loading, unloading & loadout", r"load ?out|loading|unloading|\bloader\b|bagging|bagger|packaging|packer|baler|palletiz|truck load|"
                                r"rail load|barge load|ship load|loading station|loading dock|loading area|loading facility|loading spout|"
                                r"dump station|bag dump|receiving|packing"),
    # coating, printing, solvent
    ("Printing line",           r"print|flexograph|lithograph|gravure|offset press|screen print|ink jet|inkjet|label press|web press"),
    ("Degreaser & solvent cleaning", r"degreas|parts wash|parts clean|cold clean|solvent clean|solvent wip|vapor degreas|dry clean|hand wip|"
                                r"wipe clean|wiping|cleaning (?:operation|station|machine|process|system|line|equipment)|\bwasher\b|"
                                r"wash system|washing|tank clean|solvent (?:use|usage|recovery|still)|cleaner\b|^cleaning$"),
    ("Adhesive, resin & fiberglass application", r"open mold|gel ?coat|resin|adhesive|\bglue|gluing|laminat|pultrusion|fiberglass|\bfrp\b|"
                                r"chop(?:per)? gun|hand lay|lay-?up|sealant|caulk|bonding"),
    ("Spray booth & coating line", r"booth|coating|coater|paint|spray|powder coat|primer|enamel|varnish|lacquer|finishing|roll coat|"
                                r"dip coat|electrocoat|e-coat|galvanneal|touch-?up|stain|topcoat|basecoat|clearcoat"),
    # fabrication & forming
    ("Welding & thermal cutting", r"weld|torch|plasma|thermal cut|oxy-?fuel|brazing|soldering|flame cut|arc cut"),
    ("Abrasive blasting",       r"blast|shot peen|sand ?blast|bead blast|grit blast"),
    ("Woodworking",             r"woodwork|\bsaw\b|sawing|sawmill|sander|sanding|planer|plan(?:ing|er) mill|router|routing|moulder|molder|"
                                r"edger|trim saw|chop saw|lumber|wood (?:processing|shop|waste)|denibber|veneer|plywood|particleboard|trimmer|"
                                r"carpentry|edge bander|\bshaper\b|mold shop"),
    ("Foundry & casting",       r"core (?:machine|making|oven|room|line)|shell core|shakeout|shake out|pouring|casting|caster|"
                                r"sand (?:system|handling|mixer|reclaim|mull)|mold(?:ing)? (?:line|sand)|foundry|mulling"),
    ("Plastics extrusion & molding", r"extru|injection mold|molding (?:machine|press|operation)|molding$|thermoform|blow mold|compression mold|"
                                r"rotational mold|pelletiz|plastic|polymer process|\bfoam|calender|slitter|\bfilm\b"),
    ("Metal machining, grinding & forming", r"machin|\blathe|\bcnc\b|cutting|cutter|forging|drawing|spool|wire enamel|"
                                r"metal (?:processing|forming|fabric)|fabrication|punch|shear|polish|buff|tumbl|honing|broach|\bdrill|"
                                r"threading|pipe thread|\bpress(?:es)?\b|downdraft table"),
    # chemical / petroleum process
    ("Glycol dehydrator, amine & gas treating", r"dehydrat|glycol|\bteg\b|amine|sweetening|sulfur recovery|\bsru\b|molecular sieve|gas treat|"
                                r"acid gas|claus|caustic treat|merox|gas plant|\bngl\b|fractionat"),
    ("Vapor recovery unit",     r"vapor recovery|vapour recovery|\bvru\b|vapor balanc"),
    ("Distillation column, stripper & absorber", r"distill|\bcolumn|\btower|stripper|stripping|\bstill\b|still system|absorber|absorption|"
                                r"rectif|deaerator|flash tower|air stripper"),
    ("Reactor",                 r"reactor|reaction vessel|polymeri|hydrotreat|hydrocrack|catalytic crack|\bfcc\b|coker|alkylat|isomeri|"
                                r"reform(?:ing)? unit|synthesis|digester|fluidized bed|regenerator|converter|neutraliz|causticiz|slaker|"
                                r"acetator|oxidation unit|chlorinat|nitrat|sulfonat|esterif|hydrogenat|gasifier|gasification"),
    ("Separator, process vessel & knockout", r"receiver|pressure vessel|\bdrum$|filter separator"),
    ("Wastewater, landfill & ponds", r"landfill|wastewater|waste water|\bwwtp\b|\bww\s?vent|\bpond|lagoon|clarifier|oil[/ -]?water|"
                                r"api separator|\bsump\b|sewer|septic|\bdaf\b|aeration|sludge|biosolid|compost|leachate|gas collection|"
                                r"extraction well|gas well|effluent|surface impound|drain system|process drain|\bdrains?\b|cooling pond|"
                                r"settling pond|impoundment"),
    ("Mixer, blender & kettle", r"\bmix|blend|kettle|agitat|kneader|banbury|\bpot\b|melt pot|dispers|homogeniz|emulsif"),
    ("Heat exchanger, condenser & refrigeration", r"condenser|heat exchang|chiller|refrigerat|\bcooler|cooling|air cooled|fin fan|intercooler|aftercooler"),
    ("Evaporator, centrifuge, filter & other process equipment",
                                r"evaporat|crystalliz|centrifug|rotary filter|belt filter|\bfilter\b|filtrat|dewater|cooling drum|prill|granulator|"
                                r"extractor|extraction|bleach|digest|thickener|screw press|spinning|carding|textile|heat recovery|economizer|"
                                r"vaporiz|sterili|thermo-?\s?cycler|hydrolys|fermentor|bioreactor|incubat|lyophil|freeze dry|humidif|"
                                r"agglomerat|expander|briquett|conditioner|destoner|dehull|scalper|magnet|scale\b|shaker|diverter|breaker|"
                                r"concentrator|refiner|ejector|flash tunnel|flash chamber|deodoriz|saturator|wrapper|filling"),
    # cooling & utilities
    ("Compressor, blower, fan & vacuum", r"compressor|blower|\bfan\b|exhaust fan|vacuum|aspirat|air system|pneumatic system|nitrogen generator|air dryer"),
    ("Pump",                    r"\bpump"),
    # fugitive & area
    ("Facility-wide / area source", r"facility[- ]wide|entire facility|whole facility|plant[- ]wide|site[- ]wide|area source|emission cap|"
                                r"emissions cap|\bcap\b|^facility$|^plant$|^site$|general facility|all (?:other )?(?:emission )?units"),
    ("Roads & open fugitive dust", r"\broads?\b|roadway|haul|parking|paved|unpaved|traffic|vehicle travel|wind erosion|open area|"
                                r"fugitive dust|dust (?:source|emission)|disturbed|disturbance|earth ?moving|land clearing|grading|site prep|\byard\b"),
    ("Equipment leaks & fugitive components", r"fugitive|equipment leak|\bleaks?\b|component|\bvalves?\b|flange|connector|pressure relief|"
                                r"relief device|\bprv\b|\bpsv\b|seal\b|sampling|open[- ]ended|piping|pipeline|pigging|pig launch|hatch|"
                                r"pneumatic|controller|bridle|\bwell\b|wellhead|well head"),
]

# ---- WEAK rules: generic words.  Run after STRONG on both fields. ----------
WEAK: list[tuple[str, str]] = [
    ("Fuel-burning equipment, unspecified", r"combust|\bburner|fuel[- ]burning|fired equipment|external combustion|fuel gas|natural gas$|fireplace"),
    ("Material handling, unspecified", r"material handling|materials handling|material processing|coal handling|ash handling|grain processing|"
                                r"bulk material|\bhandling\b|material storage/handling"),
    ("Emission point, stack or vent", r"\bvent|stack|emission point|emissions? unit|release point|\beps?\b|exhaust|outlet|discharge point|"
                                r"\beu\b|point source|^source$|stationary vent|\bhood|roof monitor|\binlet\b"),
    ("Laboratory, maintenance & misc activity", r"laborator|\blab\b|maintenance|repair|insignificant|trivial|miscellaneous|\bmisc\b|"
                                r"activity|activities|warehouse|building|enclosure|office|housekeeping|janitor|mobile|portable|research|"
                                r"testing|test equipment|quality control|\bqa/qc|training|^other|^n/?a$|^none$|^unknown$|permit to|"
                                r"analyzer|circuit breaker|^device$|^application$|liner|scroll|sealing|cementer|cool down|^pit$|^area$|"
                                r"inspection|staging area|glove box|scrap bay|volume source"),
    ("Generic process or production line", r"process|production|manufactur|operation|\bline\b|equipment|system|unit|machine|plant|"
                                r"\bproc\b|assembly|fabricat|material|^general$|^various$|^multiple$|^group$|station"),
    (STORAGE_,                  r"storage"),
    (LOADING_,                  r"transfer|^load"),
]

for _name, _rx in STRONG + WEAK:
    assert re.compile(_rx).groups == 0, f"capturing group in rule {_name!r}"
STRONG_C = [(n, re.compile(rx, re.I)) for n, rx in STRONG]
WEAK_C = [(n, re.compile(rx, re.I)) for n, rx in WEAK]

# Secondary rules for placeholders ------------------------------------------
COMP_DRIVE_RX = re.compile(r"compressor|pump(?:ing)?|pumpjack|pump jack|drive\b|driven|well ?head|wellhead|\blift\b|irrigation|chiller|crane|hoist|mechanical", re.I)
COMFORT_RX = re.compile(r"space|comfort|building|warehouse|office|shop|room|make-?\s?up|makeup|unit heater|radiant|infrared|tube|water heater|"
                        r"domestic|hvac|rooftop|roof top|\brtu\b|furnace \(|forced air|ceiling|wall|garage|hangar|plant heat|maintenance|parts area", re.I)
RECIP_RX = re.compile(r"reciprocat|\brice\b|piston|diesel|spark|compression", re.I)
LIQUID_RX = re.compile(r"gallon|\bgal\b|\bbbl\b|barrel|liter|litre|\bgpm\b|\bgph\b", re.I)
SOLID_RX = re.compile(r"ton|\btph\b|\blbs?\b|pound|cubic|\bcy\b|bushel|\bbu\b", re.I)
HP_RX = re.compile(r"\bb?hp\b|horsepower|\bkw\b|kilowatt|\bmw\b|megawatt|\bkva\b|\bkwe\b|\bmwe\b", re.I)
HEAT_RX = re.compile(r"btu|mmbtu|mbtu|therm|\bkj\b|\bgj\b|boiler hp", re.I)

# Combustion-flag evidence ---------------------------------------------------
BURNABLE_FUEL_RX = re.compile(
    r"natural gas|diesel|propane|fuel oil|#\s?\d|no\.? ?\d|gasoline|coal|wood|biomass|landfill gas|digester gas|biogas|fuel gas|"
    r"refinery gas|process gas|\blpg\b|butane|kerosene|jet fuel|residual|distillate|methane|hydrogen|waste oil|used oil|tire|bark|"
    r"pellet|bagasse|black liquor|coke|ethanol|methanol|syngas|producer gas|field gas|casinghead|flare gas|waste gas|tail gas|"
    r"off-?gas|\blng\b|\bcng\b|\bgas\b|\boil\b|lignite|petroleum coke|sawdust|hog fuel|agricultural waste|\bmsw\b|refuse|"
    r"(?<!no )(?<!non-)\bfuels?\b", re.I)
ELECTRIC_FUEL_RX = re.compile(r"electric|steam\b|^\s*none|^\s*n/?a\b|not applicable|no fuel|hot water|^\s*-+\s*$", re.I)
ELECTRIC_TEXT_RX = re.compile(r"\belectric(?:al)?\b(?! generat| gen\b| utility| arc weld| power| energy| motor| drive|ity gen)|"
                              r"electrically[- ]?(?:heated|powered|fired)|induction (?:furnace|heat|melt)|electric arc|\beaf\b|"
                              r"resistance heat|steam[- ](?:heated|coil|tube|jacket)|hot water coil|microwave|electron beam|\buv[- ]cur", re.I)
FIRED_TEXT_RX = re.compile(r"\bfired\b|burner|combust|mmbtu|btu/hr|btu per hour|heat input", re.I)


def normalise(s: pd.Series) -> pd.Series:
    """Lower-case, collapse whitespace, keep only the first ';'-separated value."""
    s = s.astype("string").fillna("").str.lower().str.strip()
    s = s.str.replace(r"\s+", " ", regex=True)
    return s.str.split(";").str[0].str.strip()


def _match(values: pd.Index, rules) -> pd.Series:
    """Map each distinct string to the first matching rule name ('' if none)."""
    out = pd.Series("", index=values, dtype="object")
    remaining = pd.Series(True, index=values)
    remaining[values == ""] = False
    for name, rx in rules:
        if not remaining.any():
            break
        idx = remaining[remaining].index
        hit = idx[idx.str.contains(rx)]
        out[hit] = name
        remaining[hit] = False
    return out


def _apply(fine: pd.Series, source: pd.Series, text: pd.Series, rules, label: str) -> None:
    todo = fine == ""
    if not todo.any():
        return
    sub = text[todo]
    m = _match(pd.Index(sub.unique()), rules)
    hit = sub.map(m).fillna("")
    fine[todo] = hit
    source[todo & (fine != "")] = label


def resolve_engine(ut: pd.Series, desc: pd.Series) -> pd.Series:
    """Engine duty from unit type + description, in order of specificity."""
    txt = ut + " || " + desc
    fine = pd.Series("Engine, duty unspecified", index=ut.index, dtype="object")
    turb = txt.str.contains(TURB_RX) & ~txt.str.contains(r"turbocharg", case=False) & ~ut.str.contains(RECIP_RX)
    is_fire = txt.str.contains(FIRE_RX)
    is_emerg = txt.str.contains(EMERG_RX)
    is_prime = txt.str.contains(PRIME_RX)
    is_comp = txt.str.contains(COMP_DRIVE_RX) & ~txt.str.contains(r"generat", case=False)
    fine[is_comp] = "Compressor / pump drive engine"
    fine[is_prime & ~is_comp] = "Non-emergency / prime engine"
    fine[is_emerg & ~is_prime] = "Emergency / standby generator engine"
    fine[is_fire] = "Fire pump engine"
    fine[turb] = "Combustion turbine (simple cycle / unspecified)"
    return fine


def resolve_generator(ut: pd.Series, desc: pd.Series) -> pd.Series:
    """Bare 'generator' strings: steam -> boiler, turbine -> turbine, else engine."""
    txt = ut + " || " + desc
    fine = resolve_engine(ut, desc)
    fine[txt.str.contains(r"steam gen|hot water gen", case=False)] = "Boiler"
    fine[txt.str.contains(r"smoke gen", case=False)] = "Cooker, fryer, roaster & smokehouse"
    fine[txt.str.contains(r"nitrogen gen|ozone gen|vapor gen|hydrogen gen|oxygen gen|acetylene gen", case=False)] = \
        "Evaporator, centrifuge, filter & other process equipment"
    return fine


def resolve_heater(ut: pd.Series, desc: pd.Series) -> pd.Series:
    txt = ut + " || " + desc
    fine = pd.Series("Process heater", index=ut.index, dtype="object")
    fine[txt.str.contains(COMFORT_RX)] = "Space & water heating (comfort)"
    return fine


def resolve_storage(cap: pd.Series, desc: pd.Series) -> pd.Series:
    fine = pd.Series("Storage pile & material storage", index=desc.index, dtype="object")
    liquid = cap.str.contains(LIQUID_RX) | desc.str.contains(r"tank|liquid|fuel|oil|solvent|chemical|gasoline", case=False)
    fine[liquid] = "Storage tank"
    return fine


def resolve_loading(cap: pd.Series, desc: pd.Series) -> pd.Series:
    fine = pd.Series("Bulk loading, unloading & loadout", index=desc.index, dtype="object")
    liquid = cap.str.contains(LIQUID_RX) | desc.str.contains(r"rack|gasoline|crude|condensate|fuel|oil|liquid|tanker|tank truck", case=False)
    fine[liquid] = "Liquid loading rack & fuel dispensing"
    return fine


def capacity_hint(cap: pd.Series) -> pd.Series:
    fine = pd.Series("", index=cap.index, dtype="object")
    fine[cap.str.contains(SOLID_RX)] = "Material handling, unspecified"
    fine[cap.str.contains(LIQUID_RX)] = "Storage tank"
    fine[cap.str.contains(HEAT_RX)] = "Fuel-burning equipment, unspecified"
    fine[cap.str.contains(HP_RX)] = "Engine, duty unspecified"
    return fine


def apply_placeholders(fine: pd.Series, ut: pd.Series, desc: pd.Series, cap: pd.Series) -> pd.Series:
    fine = fine.copy()
    for ph, fn in ((ENGINE_, lambda m: resolve_engine(ut[m], desc[m])),
                   (GENERATOR_, lambda m: resolve_generator(ut[m], desc[m])),
                   (HEATER_, lambda m: resolve_heater(ut[m], desc[m])),
                   (STORAGE_, lambda m: resolve_storage(cap[m], desc[m])),
                   (LOADING_, lambda m: resolve_loading(cap[m], desc[m]))):
        m = fine == ph
        if m.any():
            fine[m] = fn(m)
    return fine


def combustion_flag(fine: pd.Series, ut: pd.Series, desc: pd.Series, fuel: pd.Series, cap: pd.Series) -> pd.Series:
    flag = fine.map(FINE_TO_FLAG).astype("object")
    txt = ut + " || " + desc
    burnable = fuel.str.contains(BURNABLE_FUEL_RX)
    electric = (fuel.str.contains(ELECTRIC_FUEL_RX) | txt.str.contains(ELECTRIC_TEXT_RX)) & ~burnable
    fired_text = txt.str.contains(FIRED_TEXT_RX) | cap.str.contains(HEAT_RX)
    flag[fine.isin(ELECTRIC_OVERRIDABLE) & electric] = N
    flag[fine.isin(FUEL_OVERRIDABLE_GENERIC) & (burnable | fired_text)] = C
    flag[fine.isin(FUEL_OVERRIDABLE_FIRED_ONLY) & fired_text] = C
    blank = (ut == "") & (desc == "") & (fuel.str.strip() == "")
    flag[(fine == "Unspecified") & blank] = "unknown"
    return flag


def classify_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return the four equipment columns (OUT_COLS) for a frame with
    Unit Type, Unit Description, Capacity Unit and Fuel Type."""
    ut = normalise(df["Unit Type"])
    desc_full = df["Unit Description"].astype("string").fillna("").str.lower().str.replace(r"\s+", " ", regex=True)
    desc = desc_full.str.split(";").str[0].str.strip()
    cap = df["Capacity Unit"].astype("string").fillna("").str.lower()
    fuel = df["Fuel Type"].astype("string").fillna("").str.lower()

    fine = pd.Series("", index=df.index, dtype="object")
    source = pd.Series("", index=df.index, dtype="object")
    _apply(fine, source, ut, STRONG_C, "unit_type")
    _apply(fine, source, desc, STRONG_C, "unit_description")
    _apply(fine, source, ut, WEAK_C, "unit_type")
    _apply(fine, source, desc, WEAK_C, "unit_description")
    todo = fine == ""
    if todo.any():
        fine[todo] = capacity_hint(cap[todo])
        source[todo & (fine != "")] = "capacity_unit"

    fine = apply_placeholders(fine, ut, desc_full, cap)
    fine[fine == ""] = "Unspecified"
    source[source == ""] = "none"

    coarse = fine.map(FINE_TO_COARSE)
    missing = coarse.isna()
    if missing.any():
        raise RuntimeError(f"fine categories without taxonomy entry: {sorted(fine[missing].unique())}")
    flag = combustion_flag(fine, ut, desc_full, fuel, cap)
    return pd.DataFrame({
        "Equipment Category": coarse.astype("category"),
        "Equipment Subcategory": fine.astype("category"),
        "Combustion Source": flag.astype("category"),
        "Equipment Category Basis": source.astype("category"),
    }, index=df.index)


def taxonomy_table() -> pd.DataFrame:
    return pd.DataFrame([(v[0], k, v[1], v[2]) for k, v in TAXONOMY.items()],
                        columns=["Equipment Category", "Equipment Subcategory", "EIS unit type code", "default combustion flag"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet", default="data/processed/dashboard/permits_v5f.parquet")
    ap.add_argument("--out-dir", default="data/processed/analysis/unit_type_taxonomy")
    ap.add_argument("--sample-per-fine", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cols = ["Unit Type", "Unit Description", "Capacity Unit", "Fuel Type", "Unit ID",
            "Facility Name", "Facility State Abbreviation", "Filename"]
    df = pq.read_table(args.parquet, columns=cols).to_pandas()
    for c in cols:
        df[c] = df[c].astype("string")
    print(f"rows: {len(df):,}")

    cat = classify_frame(df)
    cat.to_parquet(out / "unit_type_categories_rows.parquet", index=False)
    taxonomy_table().to_csv(out / "taxonomy.csv", index=False)

    ut = normalise(df["Unit Type"])
    smap = (pd.DataFrame({"unit_type_norm": ut, "fine": cat["Equipment Subcategory"].astype(str),
                          "coarse": cat["Equipment Category"].astype(str)})
            .groupby(["unit_type_norm", "fine", "coarse"], observed=True).size()
            .rename("rows").reset_index().sort_values("rows", ascending=False))
    smap.to_csv(out / "unit_type_string_map.csv", index=False)

    n = len(df)
    cov_c = cat["Equipment Category"].value_counts().rename("rows").to_frame()
    cov_c["share"] = (cov_c.rows / n).round(4)
    cov_c.to_csv(out / "coverage_coarse.csv")
    cov_f = (cat.groupby(["Equipment Category", "Equipment Subcategory"], observed=True).size().rename("rows").reset_index()
             .sort_values(["Equipment Category", "rows"], ascending=[True, False]))
    cov_f["share"] = (cov_f.rows / n).round(4)
    cov_f["eis_code"] = cov_f["Equipment Subcategory"].map(FINE_TO_EIS).astype("Int64")
    cov_f.to_csv(out / "coverage_fine.csv", index=False)
    comb = pd.crosstab(cat["Equipment Category"], cat["Combustion Source"])
    comb.to_csv(out / "combustion_by_coarse.csv")
    print("\n== coarse coverage ==\n" + cov_c.to_string())
    print("\n== combustion flag ==\n" + cat["Combustion Source"].value_counts().to_string())
    print("\n== combustion by coarse ==\n" + comb.to_string())
    print("\n== match basis ==\n" + cat["Equipment Category Basis"].value_counts().to_string())

    unm = pd.DataFrame({"unit_type_norm": ut, "desc": df["Unit Description"].fillna("").str.lower().str[:80]})
    unm = unm[cat["Equipment Subcategory"].astype(str) == "Unspecified"]
    unm.unit_type_norm.value_counts().head(200).rename("rows").to_frame().to_csv(out / "unmapped_top.csv")
    unm[unm.unit_type_norm == ""].desc.value_counts().head(200).rename("rows").to_frame().to_csv(out / "unmapped_blank_type_desc_top.csv")
    print("\n== top unmapped unit-type strings ==\n" + unm.unit_type_norm.value_counts().head(30).to_string())

    rng = np.random.default_rng(args.seed)
    full = pd.concat([df, cat], axis=1)
    samp = (full.groupby("Equipment Subcategory", observed=True, group_keys=False)
            .apply(lambda g: g.sample(min(len(g), args.sample_per_fine), random_state=int(rng.integers(1 << 31)))))
    samp[OUT_COLS + ["Unit Type", "Unit Description", "Capacity Unit", "Fuel Type",
                     "Facility State Abbreviation", "Facility Name", "Filename"]].to_csv(out / "samples_by_fine.csv", index=False)
    print(f"\nwrote {out}/")


if __name__ == "__main__":
    main()
