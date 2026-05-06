"""Derive a representative NAICS code from a free-text Industry Description.

Used to recover manufacturing rows whose NAICS / SIC code columns are missing
or non-manufacturing but whose Industry Description clearly indicates a
manufacturing operation (e.g. "Petroleum Refining", "Steel mill", "Sawmill",
"Manufacturing - Blast Furnaces And Steel Mills").

Resolution order:
  1. Curated regex rules (fast, deterministic, source-controlled)
  2. JSON override cache populated by ``scripts/classify_unmatched_via_cborg.py``
     (used for free-text descriptions that don't fit the regex set; the cache
     is keyed by the lowercased+stripped description)

Downstream filters compare the returned code's first two digits to
"31"/"32"/"33" to decide whether the row is manufacturing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import pandas as pd

_CACHE_PATH = Path(__file__).resolve().parent / "data" / "description_naics_cache.json"
_cache: Optional[dict[str, Optional[str]]] = None


def _load_cache() -> dict[str, Optional[str]]:
    global _cache
    if _cache is None:
        if _CACHE_PATH.exists():
            try:
                with _CACHE_PATH.open() as f:
                    _cache = {k.strip().lower(): v for k, v in json.load(f).items()}
            except (OSError, json.JSONDecodeError):
                _cache = {}
        else:
            _cache = {}
    return _cache


def reload_cache() -> None:
    """Force re-read of the cache file (use after the script writes new entries)."""
    global _cache
    _cache = None
    _load_cache()

# (regex, NAICS) pairs. First match wins.
_RULES: list[tuple[re.Pattern, str]] = [
    # ---------------- non-manufacturing (checked first) ----------------
    # Pipeline / natural gas transport / compression / processing (treat as extraction-adjacent)
    (re.compile(r"\bpipeline\s+transportation\b", re.I), "486210"),
    (re.compile(r"\brefined\s+petroleum\s+pipelines?\b", re.I), "486910"),
    (re.compile(r"\bnatural\s+gas\b.*\b(transmission|pipeline|compressor|compress(ion)?|distribution|gathering|processing)\b", re.I), "486210"),
    (re.compile(r"\b(pipeline|compressor|compresso\s*r)\s+(station|facility|terminal)\b", re.I), "486210"),
    (re.compile(r"\bcompress\s+natural\s+gas\b", re.I), "486210"),
    (re.compile(r"\bnatural\s+gas\s+facility\b", re.I), "486210"),
    (re.compile(r"\b(o\s*&\s*g|oil\s*&\s*gas)\b.*\bcompressor\b", re.I), "486210"),
    # Electric utility / power gen
    (re.compile(r"\b(electric|electrical|electricity)\s+(services?|power\s+(generation|generating|production)|generation|generating|utility|utility\s+generating)\b", re.I), "221112"),
    (re.compile(r"\b(power|electric)\s+(plant|generation|generating|production|station)\b", re.I), "221112"),
    (re.compile(r"\bsteam\s+generation\b", re.I), "221330"),
    (re.compile(r"\bcogeneration\b", re.I), "221116"),
    (re.compile(r"\bstandby\s+electric\b", re.I), "221112"),
    (re.compile(r"\bbiomass\s+electric\b", re.I), "221117"),
    (re.compile(r"\bfossil\s+fuel\s+electric\b", re.I), "221112"),
    (re.compile(r"\bgeneration\s+of\s+electricity\b|^electricity$", re.I), "221112"),
    # Natural gas storage / transmission (additional patterns)
    (re.compile(r"\bnatural\s+gas\s+storage\b|\bliqu[ei]fied\s+natural\s+gas\b|\bstorage\s+and\s+distribution\s+of\s+natural\s+gas\b|\bcompress\s+and\s+dehydrate\s+natural\s+gas\b|\boil\s+and\s+gas\s+compression", re.I), "486210"),
    (re.compile(r"\bcrude\s+oil\s+pipelines?\b", re.I), "486910"),
    (re.compile(r"\btransmit\s+natural\s+gas\b|\bunderground\s+storage\s+fields?\b", re.I), "486210"),
    (re.compile(r"\bgas\s+processing\b.*\bsulfur\b", re.I), "211130"),
    (re.compile(r"^gas\s+processing$|\boil\s+and\s+gas\s+(industry|exploration)\b", re.I), "211130"),
    (re.compile(r"\bgas\s+transmission\b|\bgas\s+distribution\b", re.I), "486210"),
    # Data centers
    (re.compile(r"\bdata\s+cent(er|re)\b", re.I), "518210"),
    # Water / wastewater / waste
    (re.compile(r"\b(wastewater|waste\s+water|sewage|sewer|sewerage|publicly\s+owned\s+treatment\s+works|potw)\b", re.I), "221320"),
    (re.compile(r"\b(water\s+(supply|treatment|utility|plant|filtration)|drinking\s+water|potable\s+water)\b", re.I), "221310"),
    (re.compile(r"\b(landfill|refuse\s+systems|solid\s+waste|municipal\s+solid\s+waste)\b", re.I), "562212"),
    # Wholesale / storage / terminals
    (re.compile(r"\bgrain\s+elevator\b", re.I), "424510"),
    (re.compile(r"\b(petroleum|gasoline|bulk\s+gasoline)\s+(bulk\s+)?(stations?|terminals?|liquid\s+storage|storage|distribution\s+terminal|product\s+additives)\b", re.I), "424710"),
    (re.compile(r"\bbreakout\s+tank\s+farm\b", re.I), "424710"),
    # Mining / extraction
    (re.compile(r"\bcrude\s+petroleum\b|\boil\s+and\s+(natural\s+)?gas\s+(production|extraction|field\s+services)\b|\bnatural\s+gas\s+(extraction|liquid|liquids|production)\b", re.I), "211130"),
    (re.compile(r"\boffshore\s+(oil|gas)\b", re.I), "211130"),
    (re.compile(r"\b(quarry|stone\s+crushing|crushed\s+(and\s+broken\s+)?(stone|limestone))\b", re.I), "212319"),
    (re.compile(r"^mining\s*-", re.I), "212319"),
    (re.compile(r"\bunderground\s+(copper|gold|silver|coal|iron|zinc)\s+mine\b", re.I), "212230"),
    (re.compile(r"\bcoal\s+(mine|mining|surface|preparation)\b|\bbituminous\s+coal\b", re.I), "212111"),
    (re.compile(r"\bconstruction\s+sand\s+and\s+gravel\b|\bindustrial\s+sand\b|\brock\s+crushing\b|\brock,?\s+sand\s+and\s+gravel\b", re.I), "212321"),
    # Services / institutions
    (re.compile(r"\bhospitals?\b", re.I), "622110"),
    (re.compile(r"\b(college|universit(y|ies)|elementary|secondary\s+school|educational\s+institution|university\s+campus)\b", re.I), "611310"),
    (re.compile(r"\b(prison|correctional)\b", re.I), "922140"),
    (re.compile(r"\b(funeral\s+service|crematori(es|um)|funeral)\b", re.I), "812210"),
    (re.compile(r"\b(industrial\s+launder|launderer)", re.I), "812332"),
    (re.compile(r"\btesting\s+laborator", re.I), "541380"),
    (re.compile(r"\bpharmaceutical\s+research", re.I), "541714"),
    (re.compile(r"\bcommercial\s+physical\s+research\b", re.I), "541715"),
    (re.compile(r"\bnational\s+(security|defense)\b|\b(federal\s+)?military\s+(installation|operations|academy|base)\b|\bair\s+force\s+base\b|\btraining\s+center\s+for\s+military\b", re.I), "928110"),
    (re.compile(r"\beducational\s+(institution|facility)\b|\bstationary\s+educational\b", re.I), "611310"),
    (re.compile(r"\bveterinary\b", re.I), "541940"),
    (re.compile(r"\baircraft\s+services\b", re.I), "488190"),
    (re.compile(r"\bspecial\s+warehousing\b|\btransportation\s+services\b", re.I), "493190"),
    (re.compile(r"\bengine\s+testing\b", re.I), "541380"),
    (re.compile(r"\bhazardous\s+waste\b|\bwaste\s+management\b|\bmineral\s+recovery\b", re.I), "562211"),
    (re.compile(r"\bbook\s+publishing\b", re.I), "511130"),
    (re.compile(r"\begg\s+production\b", re.I), "112310"),
    (re.compile(r"\b(scientific\s+institution|research\s+and\s+development)\b", re.I), "541715"),
    (re.compile(r"\bmedical\s+(center|facility)\b", re.I), "622110"),
    (re.compile(r"\bcity\s+public\s+transport\b", re.I), "485113"),
    (re.compile(r"\b(industrial\s+launder|laundry).*\b(linen|supply|processing)?", re.I), "812332"),
    (re.compile(r"\bsolvent\s+(and\s+chemical\s+)?distribution\b", re.I), "424690"),
    (re.compile(r"\bfertilizer\s+(mixing|distribution)\b", re.I), "424910"),
    (re.compile(r"\baerospace\s+maintenance\b|\bairfield\b", re.I), "488190"),
    # Recycling / wholesale
    (re.compile(r"\b(plastic\s+recycling|materials?\s+recovery|recycling\s+facility)\b", re.I), "562920"),
    (re.compile(r"\bscrap\s+and\s+waste\s+materials\b", re.I), "423930"),
    (re.compile(r"\bbulk\s+(petroleum|chemical|gasoline)\b.*\bterminal\b", re.I), "424710"),
    (re.compile(r"\bgasoline\s+(distribution\s+)?bulk\s+terminal\b", re.I), "424710"),
    (re.compile(r"\bchemicals\s+and\s+allied\s+products\b", re.I), "424690"),
    (re.compile(r"\bpetroleum\s+(products|liquid)\s+(and\s+product\s+additives\s+)?(distribution|storage)\s+terminal\b", re.I), "424710"),
    # Other services
    (re.compile(r"\bdry\s+cleaning\b", re.I), "812320"),

    # ---------------- manufacturing: chemicals (NAICS 325) ----------------
    (re.compile(r"\b(fuel\s+)?ethanol\b.*\b(production|isobutanol|kerosene|jet\s+fuel|isooctane|from\s+corn)\b", re.I), "325193"),
    (re.compile(r"\bethanol\s+production\b", re.I), "325193"),
    (re.compile(r"\bindustrial\s+gas(es)?\b", re.I), "325120"),
    (re.compile(r"\bindustrial\s+(organic|inorganic)\s+chemicals?\b", re.I), "325199"),
    (re.compile(r"\bmetal\s+nitr(ation|ate)", re.I), "325180"),
    (re.compile(r"\bpaints?\s+and\s+allied\s+products\b|\bpaint\s+manufactur", re.I), "325510"),
    (re.compile(r"\b(geraniol|nerol|linalool|menthane|terpene|rheological\s+agent)\b", re.I), "325199"),
    (re.compile(r"^chemical\s+products?$|^chemicals?$|^chemicals,?\s+fibers,?\s+and\s+plastics$", re.I), "325199"),
    (re.compile(r"\bnitrogenous\s+fertilizer", re.I), "325311"),
    (re.compile(r"\b(agricultural\s+chemical|insecticide|pesticide)\b", re.I), "325320"),
    (re.compile(r"\b(medicinal\s+chemical|pharmaceutical|botanical\s+products?)\b", re.I), "325412"),
    (re.compile(r"\bchemical\s+preparation", re.I), "325998"),
    (re.compile(r"\bcharcoal\b", re.I), "325998"),
    (re.compile(r"\bgas\s+processing\s+(plant|sulfur)", re.I), "325120"),
    # Petroleum & coal (NAICS 324)
    (re.compile(r"\bpetroleum\s+refin", re.I), "324110"),
    (re.compile(r"\brefinery\b", re.I), "324110"),
    (re.compile(r"^refining$|\bdiesel\s+and\s+gasoline\s+production\b|\brenewable\s+fuels?\b", re.I), "324110"),
    (re.compile(r"\basphalt\s+(paving|plant|roofing|mixtures?|products?)\b", re.I), "324121"),
    (re.compile(r"\b(roofing\s+and\s+asphalt|asphalt\s+roofing)\b", re.I), "324122"),
    (re.compile(r"\bhot\s+mix\s+asphalt\b|\basphalt\b.*\bplant\b", re.I), "324121"),
    # Nonmetallic mineral (NAICS 327)
    (re.compile(r"\b(portland\s+)?cement\b", re.I), "327310"),
    (re.compile(r"\blime\b(?!stone)", re.I), "327410"),
    (re.compile(r"\bclay\s+refractor|\brefractories\b", re.I), "327120"),
    (re.compile(r"\bglass\s+(container|product)", re.I), "327213"),
    (re.compile(r"\bnonmetallic\s+mineral\s+(processing|product)", re.I), "327120"),
    (re.compile(r"\bminerals?,?\s+ground\s+or\s+treated\b", re.I), "327992"),
    (re.compile(r"\bpressed\s+and\s+blown\s+glass\b", re.I), "327212"),
    (re.compile(r"\bslag\s+processing\b", re.I), "327992"),
    (re.compile(r"\b(dimensional\s+)?granite\s+(fabricat|product)", re.I), "327991"),
    # Primary metals (NAICS 331)
    (re.compile(r"\bsteel\s+mini[-\s]*mill", re.I), "331110"),
    (re.compile(r"\b(integrated\s+)?steel\s+(mill|plant|production|finishing)", re.I), "331110"),
    (re.compile(r"\bsteel\s+(pipe|tube)", re.I), "331210"),
    (re.compile(r"\bcold\s+finishing\s+of\s+steel\b", re.I), "331221"),
    (re.compile(r"\bblast\s+furnace", re.I), "331110"),
    (re.compile(r"\b(iron\s+and\s+steel|gray\s+and\s+ductile\s+iron|gray\s+iron|ductile\s+iron|malleable\s+iron|iron|steel)\s+(foundr|forging)", re.I), "331511"),
    (re.compile(r"\bfoundry\b|\bfoundries\b", re.I), "331511"),
    (re.compile(r"\b(secondary\s+)?aluminum\s+(production|smelt|refin|cast|foundr)", re.I), "331314"),
    (re.compile(r"\baluminum\b", re.I), "331313"),
    (re.compile(r"\bprimary\s+magnesium\b", re.I), "331410"),
    (re.compile(r"\bsecondary\s+nonferrous\s+metals\b", re.I), "331492"),
    (re.compile(r"\bberyllium\s+production\b|\bzinc\s+oxide\b|\biron\s+product\b.*\belectric\s+arc\b|\belectric\s+arc\s+furnace\b", re.I), "331410"),
    # Fabricated metals (NAICS 332)
    (re.compile(r"\b(iron|steel)\s+forging", re.I), "332111"),
    (re.compile(r"\bmetal\s+(coating|surface\s+coating|treatment|plating)", re.I), "332812"),
    (re.compile(r"\bplating\s+and\s+polishing\b", re.I), "332813"),
    (re.compile(r"\bsteel\s+(fastener|processing)", re.I), "332722"),
    (re.compile(r"\bmetal\s+fabrication\b", re.I), "332710"),
    (re.compile(r"\bmetal\s+processing\b", re.I), "332810"),
    (re.compile(r"\b(metal\s+heat\s+treating|electroplating|chrome\s+plating)\b", re.I), "332813"),
    (re.compile(r"\bfabricated\s+plate\s+work\b|\bboiler\s+shop", re.I), "332313"),
    (re.compile(r"\b(metal\s+can|beverage\s+cans?)\b", re.I), "332431"),
    (re.compile(r"\bpowdered\s+iron\b", re.I), "331410"),
    (re.compile(r"\b(forge|forging|stamping|machine\s+shop|roll\s+forming|metal\s+welding)\b", re.I), "332119"),
    # Machinery (NAICS 333)
    (re.compile(r"\bconstruction\s+machinery\b", re.I), "333120"),
    (re.compile(r"\bsurface\s+coating\s+of\s+large\s+appliances?\b", re.I), "333411"),
    # Electrical equipment (NAICS 335)
    (re.compile(r"\belectric\s+motors?\b", re.I), "335312"),
    (re.compile(r"\blithium\s+battery\b|\bbattery\s+electrolyte\b", re.I), "335912"),
    # Transportation equipment (NAICS 336)
    (re.compile(r"\b(aerospace\s+(parts?|engine)|aircraft\s+(engine|parts?|assembly))", re.I), "336412"),
    (re.compile(r"\b(automotive\b.*\bassembly|automotive\s+surface\s+coating|sport\s+utility\s+vehicle\s+assembly|automobile\s+assembly|motor\s+vehicle\s+assembly|motor\s+vehicles?\s+and\s+car\s+bodies)", re.I), "336111"),
    (re.compile(r"\b(recreational\s+vehicle|rv|motorhomes?|motor\s+homes?)\b", re.I), "336213"),
    (re.compile(r"\bmotor\s+vehicle\s+(parts|accessories)\b", re.I), "336390"),
    (re.compile(r"\btruck\s+(trailers?|and\s+bus\s+bodies)\b|\b(fiberglass\s+)?utility\s+body\b", re.I), "336211"),
    (re.compile(r"\bturbo\s+remanufactur", re.I), "336412"),
    # Wood products (NAICS 321)
    (re.compile(r"\b(sawmills?\s+and\s+planing\s+mills?)\b", re.I), "321113"),
    (re.compile(r"\b(sawmill|saw\s+mill|lumber\s+mill)\b", re.I), "321113"),
    (re.compile(r"\b(millwork|hardwood\s+(dimension|flooring)|wood\s+(kitchen\s+cabinet|household\s+furniture|cabinet|furniture)|woodworking)", re.I), "321918"),
    # Paper (NAICS 322)
    (re.compile(r"\bcorrugated\s+(and\s+solid\s+fiber\s+)?box(es)?\b", re.I), "322211"),
    (re.compile(r"\bpulp\s+mill\b", re.I), "322110"),
    # Food & beverage (NAICS 311 / 312)
    (re.compile(r"\bsoybean[/\s]+oil\b|\bsoybean\s+oil\b", re.I), "311224"),
    (re.compile(r"\b(wet\s+corn\s+milling|corn\s+processing|grain\s+(mill|and\s+field\s+beans|handling|and\s+fertilizer\s+processing)|flour\s+mill|sugar\s+beet)", re.I), "311221"),
    (re.compile(r"\b(citrus\s+processing|fruit\s+processing|vegetable\s+processing)", re.I), "311421"),
    (re.compile(r"\bprepared\s+feeds?\b", re.I), "311111"),
    (re.compile(r"\b(dog|cat)\s+food\b", re.I), "311111"),
    (re.compile(r"\bbakery\b", re.I), "311812"),
    (re.compile(r"\b(winery|wineries|processing\s+and\s+storage\s+of\s+wine)\b", re.I), "312130"),
    (re.compile(r"\bmalt\s+beverages?\b|\bbrewery\b|\bbreweries\b", re.I), "312120"),
    (re.compile(r"\bdistilled\s+(and\s+blended\s+)?liquors?\b", re.I), "312140"),
    (re.compile(r"\boat\s+milling\b", re.I), "311230"),
    (re.compile(r"\bpasta\b", re.I), "311823"),
    (re.compile(r"\bsoybean\s+processing\b", re.I), "311224"),
    (re.compile(r"\b(beef|cattle|hog|poultry)\s+(slaughter|packing|rendering)\b", re.I), "311611"),
    (re.compile(r"\b(chocolate|cocoa)\b", re.I), "311351"),
    # Plastics & rubber (NAICS 326)
    (re.compile(r"\b(reinforced\s+plastic|plastic\s+composite)", re.I), "326199"),
    (re.compile(r"\bplastics?\s+products?\b", re.I), "326199"),
    (re.compile(r"\bunsaturated\s+polyester\s+resins?\b|\bplastic\s+resin", re.I), "325211"),
    (re.compile(r"\b(unsupported\s+plastics?|injection\s+molding|monofilament|resin\s+strand)\b", re.I), "326121"),
    (re.compile(r"\btires?\s+and\s+inner\s+tubes?\b|\btire\s+manufactur", re.I), "326211"),
    (re.compile(r"\bparticle\s+board\b|\bmedium\s+density\s+fiberboard\b|\bmdf\b", re.I), "321219"),
    # Printing (NAICS 323)
    (re.compile(r"\b(commercial\s+printing|book\s+printing|lithographic|printing\s+and\s+binding)", re.I), "323111"),

    # ---------------- additional non-manufacturing patterns ----------------
    (re.compile(r"\b(generating\s+station|generation\s+station|electric\s+bulk\s+power|peak\s+shaving|emergency\s+(electrical|backup)\s+(power|plant))\b", re.I), "221112"),
    (re.compile(r"\b(stationary\s+)?central\s+utility\s+plant\b|\bairport\s+central\s+energy\b|\bgeothermal\s+energy\b|\bsteam\s+plant\b|\bemergency\s+diesel\s+engine\s+generator\s+sets?\b|\belectrical\s+power\s+generators?\b|\bcoal\s+fired\s+steam\b", re.I), "221112"),
    (re.compile(r"\b(generates?|generating)\s+(and\s+transmits?\s+)?electricity\b|\bgeneration\s+and\s+transmission\s+of\s+electricity\b|\belectric\s+and\s+other\s+services\s+combined\b|\bnatural\s+gas\s+supplier\b|\bcoal\s+plant\b", re.I), "221112"),
    (re.compile(r"\b(used\s+oil\s+recycling|waste[-\s]+to[-\s]+energy|municipal\s+solid\s+waste\s+(combustion|combustors?))\b", re.I), "562213"),
    (re.compile(r"\b(scrap\s+metal\s+recycling|metal\s+recycling|paper\s+recycling|recycling\s+(facility|plant|and\s+reclamation)|storage,?\s+recycling\s+and\s+reclamation)\b", re.I), "562920"),
    (re.compile(r"\bdata\s+processing,?\s+hosting\b|\bdata\s+processing\s+services\b|\bcomputer\s+processing\s+and\s+data\s+preparation\b|\bdata\s+processing,\s+hosting,?\s+and\s+related\b", re.I), "518210"),
    (re.compile(r"\btelecommunication", re.I), "517110"),
    (re.compile(r"\bother\s+general\s+government\s+support\b|\bheadquarters\s+operations?\b", re.I), "921120"),
    (re.compile(r"\b(stationary\s+animal\s+research|research\s+facility|experimentation.*test.*research|battery\s+(development|testing)|silicon\s+wafering|higher\s+education\s+and\s+research)\b", re.I), "541715"),
    (re.compile(r"\b(real\s+estate|amusement\s+park|nursing\s+(and\s+personal\s+care\s+)?facility|skilled\s+nurse|funeral|institution\s+of\s+higher\s+education|academic\s+institution|stationary\s+high\s+school|public\s+higher\s+education|large\s+medical\s+school|healthcare\s+(services|linen)|photographic\s+studio)\b", re.I), "611310"),
    (re.compile(r"\b(stationary\s+air\s+courier|musical\s+instrument.*retail|musical\s+instrument.*distribution\s+center|airfield|aerospace\s+maintenance|aircraft\s+services|airplane\s+component.*restoration|railroad\s+car\s+repair|general\s+automotive\s+repair|metal\s+parts\s+repair|refurbish\s+industrial\s+equipment|other\s+warehousing|warehousing\s+and\s+storage|coal\s+transfer)\b", re.I), "488190"),
    (re.compile(r"\b(open[-\s]*pit|underground)\s+(copper|gold|silver|platinum|palladium|iron|zinc|coal|uranium)\b|\btalc\s+mine\b|\btaconite\s+ore\b|\bsapphire\b|\bopen\s+pit\s+gold\b|\bunderground\s+mine\s+and\s+surface\s+ore\b|\bopen-pit\s+copper", re.I), "212230"),
    (re.compile(r"\bpotash\b|\blangbeinite\b|\bsoda\s+ash\b|\btrona\s+ore\b", re.I), "212391"),
    (re.compile(r"\b(brick,?\s+stone|coal\s+and\s+other\s+minerals|chemicals\s+and\s+allied|petroleum\s+and\s+chemical\s+bulk|chemical\s+distribution|solvent\s+(blending\s+and\s+)?distribution|distributes\s+industrial\s+chemicals|fertilizer\s+(warehouse|mixing|blending\s+and\s+distribution|handling))\b", re.I), "424690"),
    (re.compile(r"\b(buying,?\s+storing|grain\s+(handling|elevator|processing,?\s+handling)|grain\s+and\s+fertilizer\s+handling)\b", re.I), "424510"),
    (re.compile(r"\b(petroleum|gasoline|fuel|crude\s+oil|petroleum\s+coke).*\b(bulk\s+loading|terminal|loading\s+facility|storage\s+(and\s+(loading|distribution|transfer))|dispensing|unloading|fuel\s+storage|product\s+storage|products\s+terminal|coke\s+calcining|product\s+bulk|distribution|transport\s+loading)\b", re.I), "424710"),
    (re.compile(r"\bbulk\s+(gasoline|liquid\s+fuel|fuel\s+storage)\b|\b(petroleum\s+)?fuel\s+storage\b", re.I), "424710"),
    (re.compile(r"\bmarine\s+terminal\b|\bfly\s+ash\s+receiving\s+and\s+distribution\b", re.I), "424710"),
    (re.compile(r"\bagricultural\s+crop\b|\bornamental\s+nursery\b|\bcotton\s+ginning\b|\bdairy\s+farm\b|\bbiogas\b|\banaerobic\s+digestion\b", re.I), "111998"),
    (re.compile(r"\b(treatment,?\s+storage,?\s+and\s+disposal\s+of\s+hazardous|hazardous\s+and\s+solid\s+wastes)\b", re.I), "562211"),
    (re.compile(r"\bpipelines?,?\s+nec\b|\bdistribution\s+of\s+natural\s+gas\b|\bnatural\s+gas\s+supplier\b|\bgas\s+treatment\b|\bgas\s+treating\s+plant\b|\bco2\s+(removal|sequestration)\b|\bcoalbed\s+methane\b|\bsour\s+gas\b|\bgas\s+plant\b", re.I), "486210"),
    (re.compile(r"\bground[-\s]*based\s+radars?\b|\bdefense\b|\blogistical\s+and\s+maintenance\s+support|\bu\.?s\.?\s+marine\s+corps\b|\bmilitary\s+aviation\b|\bnavy\b", re.I), "928110"),

    # ---------------- additional manufacturing rules ----------------
    # Food processing (NAICS 311) — generic patterns
    (re.compile(r"\b(food\s+processing|food\s+preparations?|baking|baked\s+goods|bread,?\s+cake|wholesale\s+bread|sausages?|meat\s+(processing|packing)|poultry\s+(processing|slaughter(ing)?)|slaughter(ing)?(\s+and\s+processing)?|chicken\s+processing|wholesale\s+provider\s+of\s+fresh\s+cuts|dairy\s+products|milk\s+processing|canned\s+fruits|cheese|tetra\s+pak|breadcrumb|bread\s+crumb|prepared\s+meats?)\b", re.I), "311999"),
    (re.compile(r"\b(corn\s+wet\s+milling|wet\s+corn|industrial\s+starch|modified\s+corn\s+starch|barley\s+malting|hybrid\s+grain\s+seed|corn\s+flour|grain\s+seed\s+processing|gluten\s+meal|gluten\s+feed)\b", re.I), "311221"),
    (re.compile(r"\b(soybean\s+(processing|receiving)|vegetable\s+oil\s+mill|wheat\s+germ|hexane\s+oil\s+extraction|edible\s+oil)\b", re.I), "311224"),
    (re.compile(r"\b(experimental\s+pet\s+food|pet\s+litter|prepared\s+feed|poultry\s+feed|animal\s+protein|rendering\s+plant)\b", re.I), "311111"),
    (re.compile(r"\b(egg\s+white|tortilla|wrap\s+production)\b", re.I), "311830"),
    (re.compile(r"\bcigar(s|ette)?\b", re.I), "312220"),
    (re.compile(r"\b(liquor\s+bottling|distilled\s+spirits|progressive\s+adult\s+beverages|barrel\s+toasting)\b", re.I), "312140"),
    # Wood / paper
    (re.compile(r"\b(lumber|sawmills?\s*$|sawmill|saw\s+mill|plywood|veneer|timber|southern\s+yellow\s+pine|lumber\s+processing|wood\s+products\s+and\s+components)\b", re.I), "321113"),
    (re.compile(r"\b(wood\s+office\s+furniture)\b", re.I), "337211"),
    (re.compile(r"\b(paper\s+mills?|sanitary\s+paper|pulp\s+and\s+paper|folding\s+cartons|stationary\s+paper\s+coating|paper\s+coating\s+and\s+laminating|paper\s+coating\s+and\s+metalizing|pressure\s+sensitive\s+paper|paper\s+production)\b", re.I), "322110"),
    # Printing
    (re.compile(r"\b(yearbook\s+printing|stationary\s+(flexographic|printed)\s+(printing|packaging)|flexographic\s+printing|on[-\s]*demand\s+digital\s+printing|printing\s+press\s+installation|flexible\s+packaging.*printing)\b", re.I), "323111"),
    # Petroleum / coal
    (re.compile(r"\b(crude\s+oil\s+refining|biorefining|refined\s+petroleum\s+products|denatured\s+ethanol|fuel\s+ethanol|drymillethanolplant|ethanol\s+plant|renewables?\s+fuels?\s+production|renewable\s+fuels?|petroleum[-\s]*based\s+fuels|petroleum\s+products?\s*$)\b", re.I), "324110"),
    (re.compile(r"\b(asphalt\s+felts|hot\s+mix\s+pavement|roofing\s+products)\b", re.I), "324122"),
    (re.compile(r"\blubricating\s+(oils?|greases?)\b", re.I), "324191"),
    # Chemicals
    (re.compile(r"\b(catalyst\s+plant|methyl\s+bromide|hydrogen\s+production|polymer\s+production|polymers?\s+production|industrial\s+(organic|inorganic)\s+chemicals?,?\s+nec|chemical\s+production\s+facility|produces\s+(numerous\s+)?chemical\s+products|fluorochemicals|adhesives?\s+and\s+sealants|industrial\s+bonding\s+adhesives?|petroleum\s+coke\s+processing|metallurgical\s+coke|aerosol\s+can\s+packager)\b", re.I), "325199"),
    (re.compile(r"\b(phosphate\s+fertilizer|phosphoric\s+acid|ammonium\s+nitrate|ammonia\s+(plant|storage)|nitric\s+acid|urea\s+plant|cheyenne\s+plant|hidan|lodan)\b", re.I), "325311"),
    (re.compile(r"\b(paints?,?\s+(varnish|lacquer|enamel)|paints?,?\s+varnishes,?\s+lacquers,?\s+enamels?|polyester\s+film|insulating\s+systems?|paints?\s+and\s+allied)\b", re.I), "325510"),
    (re.compile(r"\b(produces\s+modified\s+corn\s+starches|gluten\s+meal\s+production)\b", re.I), "311221"),
    (re.compile(r"\bpigments?\b|\bcolorant", re.I), "325130"),
    # Plastics & rubber (NAICS 326) — additions
    (re.compile(r"\b(extruded\s+polystyrene|polypropylene\s+compounding|polyethylene|plastic\s+resin|plastic\s+mixing\s+and\s+extrusion|stationary\s+plastic\s+(extrusion|to\s+fuel)|fiberglass|fiber\s+glass|fiber\s+insulation|plastic\s+film|plastics?,?\s+foam\s+products?|bags?\s*-?\s*plastics?|polystyrene\s+foam|epdm\s+rubber|polyurethane\s+foam|fiber\s+glass\s+insulation|fiber\s+insulating\s+and\s+sound\s+deadening|molded\s+plastic|insulating\s+systems)\b", re.I), "326199"),
    (re.compile(r"\b(open\s+and\s+closed\s+mold\s+fiberglass|fiberglass\s+hatch|fiberglass\s+utility|fiberglass\s+component|polyester\s+resin)\b", re.I), "326150"),
    (re.compile(r"\b(stationary\s+ammonia\s+storage)\b", re.I), "424690"),
    # Nonmetallic mineral (NAICS 327) — additions
    (re.compile(r"\b(stone\s+processing|silica\s+sand|kaolin\s+processing|talc\s+processing|inert,?\s+non[-\s]*metallic\s+minerals?|non[-\s]*metallic\s+mineral\s+products|aggregate\s+crushing|expanded\s+shale|finely\s+ground\s+calcium\s+carbonate|brick\s+and\s+structural\s+clay|ready\s+mix\s+concrete|carbon\s+and\s+graphite|vitreous\s+bonded\s+abrasive|slag\s+grinder|slag\s+screening|mulch\s+and\s+topsoil|mineral\s+products\s+processing)\b", re.I), "327999"),
    (re.compile(r"\b(limestone\s+(crushing|grinding)|crushing\s+and\s+sizing\s+operation)\b", re.I), "327992"),
    # Primary metals (NAICS 331)
    (re.compile(r"\b(secondary\s+(lead|copper|magnesium|nonferrous)\s+(smelt|smelting|recycling)|secondary\s+lead\s+smelting\s+and\s+refining|copper\s+(rod|rolling|drawing|casting)|zirconium|hafnium|stationary\s+slag,?\s+metal|cleveland[-\s]*cliffs|iron\s+and\s+steel\s+making|steel\s+rolling\s+mill|stationary\s+coil\s+steel|steel\s+coil\s+pickling|stationary\s+cold[-\s]*rolled\s+steel|cold\s+drawn\s+wire|steel\s+wire|recycling\s+plant\s+and\s+rolling\s+mill|smelt(s|ing)|cupola\s+furnace|gunite|gray\s+and\s+ductile\s+iron\s+casting|primary\s+metal\s+products|gray\s+iron\s+casting)\b", re.I), "331492"),
    # Fabricated metals (NAICS 332)
    (re.compile(r"\b(metal\s+cans?|aerosol\s+can|miscellaneous\s+metal\s+work|fabricated\s+structural\s+metal|prefabricated\s+metal\s+building|fabricated\s+metal\s+products|metal\s+fabricating|metal\s+parts|fabricated\s+metal\s+parts|industrial\s+valves?|coating\s+of\s+miscellaneous\s+metal\s+parts|stationary\s+metal\s+(finishing|electroless|nitration)|electroless\s+and\s+chromate|metal\s+work\b|stationary\s+trailer\s+frame|trailer\s+frame|powder\s+coating|automotive\s+condenser|radiator|cooling\s+module|electrical\s+wire\s+and\s+cable)\b", re.I), "332710"),
    (re.compile(r"\b(stationary\s+conversion\s+kit\s+surface\s+coating|automotive\s+brake|wood\s+products\s+and\s+components\s+coating|surface\s+coating\s+of\s+plastic\s+parts|stationary\s+automotive\s+(parts\s+)?surface\s+coating|stationary\s+epdm|stationary\s+automotive\s+painting|surface\s+coating\s+operation|stationary\s+military\s+vehicle|plastic\s+and\s+metal\s+parts\s+coating|painting\s+plastic\s+and\s+metal|magnet\s+wire\s+coating|stationary\s+magnet\s+wire)\b", re.I), "332812"),
    # Machinery (NAICS 333) / Industrial Trucks (333924)
    (re.compile(r"\b(industrial\s+trucks|tractors|trailers,?\s+&\s+stackers|heating\s+equipment,?\s+except\s+electric|refurbish\s+industrial\s+equipment|industrial\s+equipment)\b", re.I), "333924"),
    # Electrical equipment (NAICS 335)
    (re.compile(r"\b(carbon\s+and\s+graphite|lead\s+acid\s+battery|battery\s+(development|testing|electrolyte)|magnet\s+wire)\b", re.I), "335912"),
    # Transportation equipment (NAICS 336)
    (re.compile(r"\b(auto\s+assembly|automobile\s+(body|assembly)|vehicle\s+production|automotive\s*$|locomotive\s+(engine|motors)|rail\s*car\s+components|truck\s+(transmission|differential|steering)|automotive\s+motor\s+component|stationary\s+automotive\s+parts)\b", re.I), "336111"),
    # Defense / pharma intermediates
    (re.compile(r"\b(uranium\s+enrichment)\b", re.I), "325180"),
    # Wood / furniture (continued)
    (re.compile(r"\b(wooden\s+gun\s+stocks?)\b", re.I), "337215"),
    # Misc mfg (NAICS 339)
    (re.compile(r"\b(surgical\s+and\s+medical\s+instruments|dental\s+equipment|medical\s+devices?)\b", re.I), "339112"),
    (re.compile(r"\b(stationary\s+musical\s+instrument|musical\s+instrument\s+and\s+pro\s+audio)\b", re.I), "339992"),
    # Misc petroleum derivatives going to mfg
    (re.compile(r"\bgas\s+to\s+propane\b|\belk\s+basin\b|\bsweet\s+gas\b|\bproduces?\s+propane\b", re.I), "211130"),
    # Photographic / measuring instruments
    (re.compile(r"\bphotographic\s+(equipment|supplies)\b", re.I), "333316"),

    # ---------------- secondary patterns / typo-tolerant ----------------
    (re.compile(r"\b(coating,?\s+printing,?\s+plating|printing\s+and\s+plating|plating\s+operations?)\b", re.I), "332813"),
    (re.compile(r"\bpellets?\b", re.I), "327992"),
    (re.compile(r"\b(slag\s+granulating|slag\s+pelletizing|slag\s+grinder)\b", re.I), "327992"),
    (re.compile(r"\brheological\s+agent", re.I), "325199"),
    (re.compile(r"\bmining\s+and\s+quarrying\b|\bsand\s+and\s+gravel\s+(operation|plant)\b", re.I), "212321"),
    (re.compile(r"\bpetroleum\s+facility\b|\boil[-\s]*gas\s+(production|industry|processing)\b|\boil\s+and\s+gas\s+(processing|central\s+tank|industry)\b|\boil\s+and\s+gas\s+central\s+tank\b", re.I), "211130"),
    (re.compile(r"\b(receipt|receiving),?\s+storage,?\s+and\s+(distribution|shipping)\s+of\s+petroleum\b|\bfor[-\s]*hire\s+hydrocarbon\b|\bhydrocarbon\s+and\s+chemical\s+product\s+storage\b", re.I), "424710"),
    (re.compile(r"\bcold[\s‑-]?rolled\s+steel\b|\bcold\s+rolled\s+steel\b", re.I), "331221"),
    (re.compile(r"\brailroad\s+equipment\b", re.I), "336510"),
    (re.compile(r"\btimber\s*strand|\btimberstrand\b", re.I), "321113"),
    (re.compile(r"\belectrical?\s+power\s*ge\s*nerating\s+station\b|\bge\s*nerating\s+station\b", re.I), "221112"),
    (re.compile(r"\bautomotive\s+(condenser|radiator|brake|cooling)|assembly\s+and\s+testing\s+facility\s+for\s+automotive|automotive\s+motor\s+component", re.I), "336390"),
    (re.compile(r"\bcommodity\s+fumigation\b|\bmethyl\s+bromide\s+fumigation\b", re.I), "115112"),
    (re.compile(r"\bbulk\s+transport\s+loading\s+facility\b|\bgasoline.*\bbulk\s+loading\b|\bgasoline.*\bloading\s+facility\b|\bgasoline/diesel\s+bulk\s+loading\b", re.I), "424710"),
    (re.compile(r"\bautobody\s+(repair|refinishing)\b|\bautomobile\s+repair\b|\bgeneral\s+automotive\s+repair\b", re.I), "811121"),
    (re.compile(r"\bconverting\s+(the\s+)?raw\s+sulfur\s+compounds?\b|\bsulfur\s+(compounds|recovery)\b", re.I), "211130"),
    (re.compile(r"\bnon[-\s]*metallic\s+mineral\s+processing\b", re.I), "327999"),
    (re.compile(r"\bjob\s+shop\s+for\s+coating\b|\bindustrial\s+painting\s+specialist\b", re.I), "332812"),
    (re.compile(r"\bplastics?\s+to\s+fuel\b|\belectronic\s+recycling\b", re.I), "562920"),
    (re.compile(r"\bprocessing\s+and\s+packaging\b|\bcontract\s+packaging\b|\bsanitation\s+contract\s+packaging\b", re.I), "561910"),
    (re.compile(r"\bmunicipal\s+solid\s+waste\b.*\b(combustion|resource\s+recovery)\b", re.I), "562213"),
    (re.compile(r"\bgas\s+processing\s+plan(?!t)|\bgas\s+plan\b", re.I), "211130"),
    (re.compile(r"\bcultured\s+marble\b", re.I), "327991"),
    (re.compile(r"\bengine\s+service\s+training\b|\bengine\s+testing\b", re.I), "541380"),
    (re.compile(r"\belectrical?\s+wire\s+and\s+cable\b", re.I), "335929"),
    (re.compile(r"\bfluoroproducts?\b|\bfluorochemicals?\b", re.I), "325199"),
    (re.compile(r"\binlet\s+gas\s+enters\s+the\s+plant\b|\bsales\s+pipeline\b", re.I), "486210"),
    # More long-tail patterns
    (re.compile(r"\bmetal\s+cleaning\s+and\s+recycling\b|\bnon[-\s]*ferrous\s+metals\s+recycling\b|\be[-\s]*scrap\s+recycling\b|\bdemolition\s+debris\b|\bsolid\s+waste\s+and\s+resource\s+recovery\b", re.I), "562920"),
    (re.compile(r"\bprefabricated\s+metal\s+buildings?\b|\bmiscellaneous\s+fabricated\s+wire\b", re.I), "332710"),
    (re.compile(r"\bflour\s+and\s+mill\s+feed\b|\broasted\s+coffee\b|\bmill\s+feed\b", re.I), "311221"),
    (re.compile(r"\brubber\s+(replacement|original\s+equipment\s+parts)\b", re.I), "326291"),
    (re.compile(r"\bexplosives?\s+and\s+munitions?\b|\bmunitions?\b", re.I), "325920"),
    (re.compile(r"\binstitutional\s+medical\s+campus\b|\bmedical\s+campus\b|\btheme\s+park|\bresort", re.I), "713110"),
    (re.compile(r"\bprinting\s+plant\b", re.I), "323111"),
    (re.compile(r"\bother\s+airport\s+operations|\bcorporate\s+hangar\b|\brailway\s+support\b|\brental\s+of\s+railcars\b", re.I), "488119"),
    (re.compile(r"\binsurance\s+services\b", re.I), "524113"),
    (re.compile(r"\bmulti[-\s]*fuel\s+engine\s+test\b", re.I), "541380"),
    (re.compile(r"\brefrigeration\s+and\s+heating\s+equipment\b", re.I), "333415"),
    (re.compile(r"\bconcrete\s+products\b", re.I), "327390"),
    (re.compile(r"\bformaldehyde\b|\bthermoset\s+resin\b", re.I), "325211"),
    (re.compile(r"\bbattery\s+manufat?cturing\b|\blead\s+acid\s+battery\s+manufat", re.I), "335912"),

    # ---------------- final batch of patterns (broad coverage) ----------------
    # Steel / iron / metals
    (re.compile(r"\bsteel\s+(works|coil\s+finishing|pickling|plate)|\bcoil\s+coating|\bsteel\s+stock\b|\bproduction\s+of\s+coke\b|\bcoke\s+screening\b|\bcoke,?\s+limited\s+coal\b|\bover\s+the\s+road\s+tractor.trailer\s+components|\biron\s+and\s+steel\s+recycling\b|\bferrous\s+and\s+nonferrous\s+investment\s+casting\b|\binvestment\s+casting\b|\bmolten\s+iron\b|\bmolten\s+steel\b|\bhot\s+rolled\s+steel\b|\bsteel\s+slabs?\b|\bsteel\s+coils?\b|\bsteel\s+plates?\b|\bcoated\s+steel\s+sheet\b", re.I), "331110"),
    (re.compile(r"\b(metal\s+anodizing|metal\s+finishing|metal\s+(coil\s+|mo\s*lding\s+and\s+)?(finishing|coating)|anodizing,?\s+and\s+surface\s+coating|architectural\s+metal\s+products|fabricates?\s+structural\s+steel|miscellaneous\s+metal\s+working|bridge\s+beam\s+fabrication|nonferrous\s+wire|drawing\s+&?\s+insulating\s+of\s+nonferrous\s+wire|stationary\s+motor\s+and\s+copper\s+wire|copper\s+wire\s+surface\s+coating|electrical\s+enclosures\s+fabrication|stationary\s+electrical\s+enclosures|metal\s+components\s+for\s+aerospace|gas\s+turbine.*high.precision|engine\s+emission\s+control\s+system|stationary\s+engine\s+emission\s+control|consumer\s+product\s+packaging|crumb\s+rubber|aerospace.*high.precision|stationary\s+facility\s+producing\s+metal\s+components|copper\s+wire)\b", re.I), "332710"),
    # Plastics & rubber (NAICS 326)
    (re.compile(r"\b(custom\s+compound(ing)?\s+(of\s+)?purchased\s+(plastics?\s+)?resins?|plastic\s+resin\s+(custom\s+)?compounding|polymer\s+casting|sink,?\s+counter\s+top\s+and\s+shower|expanded\s+polystyrene|expandable\s+polystyrene|polystyrene\s+foam|foam\s+food\s+tray|stationary\s+plastic\s+thermoforming|thermoforming\s+and\s+rotational|rotational\s+molding|polyurethane\s+spray|spray\s+in\s+bed\s+liner|polyvinyl\s+alcohol|pvoh|pvc\s+production|fluoropolymers?|laminated\s+plastics|plastic\s+pallets?|stationary\s+polyurethane|recycler\s+of\s+plasc?\s+resin|plastic\s+parts\s+coating|stationary\s+plastic\s+parts|blow\s+mold|carboard?\s+tube|extruded?\s+polystyrene)\b", re.I), "326199"),
    # Wood
    (re.compile(r"\b(wood\s+products|wooden\s+drawer\s+box|wood\s+production|gypsum\s+rock|wallboard|gypsum\s+products|creosote\s+solution|pressure\s+treatment\s+of\s+wood|american\s+wood\s+protection)\b", re.I), "321999"),
    # Paper / printing
    (re.compile(r"\b(folding\s+paperboard|corrugated\s+supplies|stationary\s+packaging\s+rotogravure|rotogravure\s+printing|sheetfed\s+offset\s+lithography|sheetfed\s+offset|web\s+offset\s+printing|offset\s+lithograph|offset\s+printing|flexographic\s+and\s+rotogravure|prints?\s+packaging\s+products?|printing\s+operation.*paper|printing\s+of\s+diplomas)\b", re.I), "323111"),
    # Stone / minerals / aggregate / mining
    (re.compile(r"\b(silica\s+mining|silica\s+sand\s+mining|silica\s+mining\s+and\s+processing|aggregate\s+production|sand\s+and\s+gravel\s+aggregate|process\s+aggregate|construction\s+industry\s*-\s*aggregate|rock,?\s+sand,?\s+&\s+gravel|micro\s+pelletizing|stationary\s+micro\s+pelletizing|bentonite\s+clay|stationary\s+sand\s+and\s+mineral|sand\s+and\s+mineral\s+mixing|lightweight\s+aggregate|lightweight\s+expanded\s+shale|shale\s+processing|coal\s+grinding|coal\s+grinding\s+and\s+clay|porcelain\s+electrical|refractor)\b", re.I), "327999"),
    (re.compile(r"\b(mining\s+operations?|mining,?\s+extraction|mining\s+extraction\s+and\s+refining\s+of\s+gold\s+and\s+silver\s+ore|gold\s+and\s+silver\s+ore|silica\s+mining)\b", re.I), "212230"),
    # Petroleum / chemicals
    (re.compile(r"\b(asphaltic\s+concrete\s+plant|hydrogen\s+reformer|natural\s+gas\s+sweetening|gas\s+sweetening|gas\s+treating\s+and\s+processing|gas\s+treating\s+plant|gas\s+cycling|gas\s+plant|carbon\s+dioxide\s+recovery|crude\s+oil\s+pumping|enhanced\s+oil\s+recovery|natural\s+gas\s+sweetening/liquids|liquids\s+extraction|stationary\s+gasification|gasification\s+plant|fluid\s+catalytic\s+cracking|fccu|alkylation|dimersol|cyclic\s+crudes|heavy\s+oil\s+western|crude\s+oil\s+and\s+natural\s+gas\s+producer|produced\s+water\s+management|oilfield\s+waste|gas\s+processing\s+facility|oil\s+and\s+ngl\s+stabilization|inorganic\s+chemicals\s+for\s+use\s+in\s+the\s+petroleum)\b", re.I), "211130"),
    (re.compile(r"\b(distillery|tequila|distilled\s+spirits|barrel\s+toasting|cereal\s+breakfast\s+foods)\b", re.I), "311230"),
    (re.compile(r"\b(rice\s+cake|coffee\s+roasting|roasted\s+coffee|food\s+industry|food\s+grade\s+ammonium|food\s+grade\b|potato\s+processing|protein\s+meal|byproduct\s+rendering|used\s+cooking\s+oil|rendering|secondary\s+protein\s+nutrient|spn\s+fines|animal\s+feed\s+mill|stationary\s+animal\s+feed|consumer\s+product\s+packaging\s+plant|agricultural\s+products?\s+processing|dry\s+distillers\s+grains|wholesale\s+bread\s+baking|vegetable\s+dehydration)\b", re.I), "311999"),
    (re.compile(r"\b(carbon\s+dioxide\s+recovery|fluoroproduct|chemical\s+plant\s+producing|calcium\s+hypochlorite|formaldehyde|thermoset\s+resins?|agricultural\s+and\s+industrial\s+nitrogen|inorganic\s+chemicals|hemp[-\s]*based\s+cbd|cbd\s+oil\s+extraction|fluoropolymers?|polymer\s+production|food\s+grade\s+ammonium|food\s+grade\s+(sodium|magnesium)\s+bisulfite)\b", re.I), "325199"),
    # Combined heat and power / utilities
    (re.compile(r"\b(combined\s+heat\s+and\s+power|electric\s+power\s+and\s+steam|electricity\s+and\s+steam|electric\s+energy\s+generation|electric\s+power\b|electrical?\s+power\b|electric\s+utilities|fossil\s+fuel\s+fired\s+steam|combustion\s+turbine\s+facility|generation\s+of\s+electric\s+energy|stationary\s+reciprocating\s+internal\s+combustion|internal\s+combustion\s+engines|fluid\s+catalytic|heating\s+plants?\s+and\s+generator|heating\s+plants?|generator\s+sets?)\b", re.I), "221112"),
    # Research / education / services
    (re.compile(r"\b(commercial\s+physical\s+(and|&)\s+biological\s+research|noncommercial\s+research|engine\s+research|research\s+&?\s+engineering|space\s+surveillance|computer\s+network\s+operations|electronic\s+parts\s+distributor|distributor\s+of\s+electronic\s+parts|process\s+control\s+instruments|advanced\s+semiconductor\s+packaging|hbm\s+fabrication|high.bandwidth\s+memory|health\s+sciences|education\b|educational\s+services|health[-\s]*care\s+facility|hotels?\s+and\s+motels?|services\s*-\s*business\s+services|sanitary\s+services|business\s+services|repair\s+shops|automotive\s+repair\s+shops|farm\s+machinery|conveyor\s+and\s+conveyor|pumps?\s+and\s+pumping|marine\s+cargo\s+handling|warehouse\s+storage\s+and\s+distribution|recycling\s+and\s+disposal|biosolid\s+handling|grain\s+terminal\s+elevator|grain\s+cleaning\s+and\s+transfer|barge\s+loading\s+grain\s+terminal|stationary\s+national\s+processing\s+center|landing\s+gear\s+services|aircraft\s+overhaul|aircraft\s+maintenance|airfield|engine\s+research|military\s+training)\b", re.I), "541715"),
    (re.compile(r"\b(turkey\s+breeding|breeding\s+operation|stationary\s+gypsum)\b", re.I), "112310"),
    # Vehicle assembly / components
    (re.compile(r"\b(travel\s+trailer\s+assembly|automobile\s+and\s+light.duty\s+truck\s+assembly|passenger\s+vehicle\s+assembly|stationary\s+plant\s+for\s+producing\s+transmissions|producing\s+transmissions\s+for\s+use\s+in\s+automobile|electronic\s+components\s+producer.*automotive|railroad\s+car\s+shop|railcar\s+welding|railcar\s+fabrication|railcar\s+repair|railcar\s+repainting|trailer\s+repair|automobile\s+refinishing|wheelchair\s+accessible\s+vans|auto\s+collision\s+repair|aerosol\s+packaging|aerosol,?\s+liquid,?\s+and\s+dry\s+packaging|automotive\s+coating|automotive\s+painting|marine\s+parts.*coating|turbine\s+generator\s+blades|stationary\s+polyurethane.*spray|polyurethane\s+spray.*bed\s+liner|finishes\s+over\s+the\s+road)\b", re.I), "336111"),
    (re.compile(r"\barmature\s+rewinding\b", re.I), "811310"),
    (re.compile(r"\bshipyard\b|\bsubmarine\s+repair\b|\bnaval\s+submarines\b", re.I), "336611"),
    # Bulk / storage
    (re.compile(r"\b(for[-\s]+hire\s+bulk\s+liquids|bulk\s+liquid\s+storage|bulk\s+product\s+terminals?|organic\s+liquids\s+distribution|crude\s+oil\s+pumping|grain\s+terminal\s+elevator|grain\s+cleaning\s+and\s+transfer|barge\s+loading\s+grain\s+terminal|warehouse\s+storage\s+and\s+distribution|logistics\s+center|marine\s+cargo|stationary\s+ammonia\s+storage)\b", re.I), "424710"),
    # Recycling / waste
    (re.compile(r"\b(crumb\s+rubber.*tire\s+recycling|tire\s+(metal\s+and\s+)?rubber\s+recycling|tire\s+recycling|lithium\s+ion\s+battery\s+recycling|lithium\s+battery\s+recycling|recycling\s+and\s+disposal\s+facility|stationary\s+plastics?[-\s]*to[-\s]*fuel|recycling\b)\b", re.I), "562920"),
    (re.compile(r"\b(flood\s+control\s+pump)\b", re.I), "221310"),
    # Healthcare / education / lodging
    (re.compile(r"\b(institutional\s+medical\s+campus|medical\s+campus|health[-\s]*care|hotel|motel|noncommercial\s+research)\b", re.I), "611310"),
    # Concrete (alone)
    (re.compile(r"^concrete$|\bconcrete\s+products|\bcellulose\s+insulation\b", re.I), "327390"),
    # Wine storage
    (re.compile(r"\bwine\s+storage\b", re.I), "493130"),
    # Renewable / biorefining
    (re.compile(r"\b(renewable\s+(natural\s+gas|diesel\s+production)|organic\s+digester\s+renewable|organic\s+digester|biochar|pipeline.quality\s+renewable\s+natural\s+gas)\b", re.I), "325193"),
    # Misc additions
    (re.compile(r"\brefractor(y|ies)\s+(shape\s+)?production|refractor(y|ies)\s+production\b", re.I), "327120"),
    (re.compile(r"\bfeed\s+additives?\s+for\s+(domestic\s+)?animals?\b", re.I), "311111"),
    (re.compile(r"\bthermal\s+spray\s+(and\s+finishing)?\s*shop?", re.I), "332813"),
    (re.compile(r"\bsynthetic\s+fiber\s+production|lyocell|cellulose\s+fiber\b", re.I), "313310"),
    (re.compile(r"\bhydrocarbon\s+bulk\s+terminal\b", re.I), "424710"),
    (re.compile(r"\bnaval\s+air\s+station\b|\bmaximum\s+security\s+detention\b|\bdetention\s+center\b", re.I), "928110"),
    (re.compile(r"\bseparates,?\s+compresses,?\s+and\s+dries\s+natural\s+gas\b", re.I), "486210"),
    (re.compile(r"\bmagnetic\s+wire\s+coating\b", re.I), "335929"),
    (re.compile(r"\bcommercial\s+refrigeration\s+equipment\b|\bcooling\s+coils|\bsteam\s+coils|\bair\s+handling\s+equipment\b", re.I), "333415"),
    (re.compile(r"\bcity[-\s]*owned\s+utility|\butility\s+company\b", re.I), "221112"),
    (re.compile(r"\bphosphate\s+ore\s+processing\b", re.I), "212393"),
    (re.compile(r"\bmineral\s+materials\s+into\s+powders\b", re.I), "327999"),
    # Generic catch-all: keywords that strongly suggest manufacturing
    # (production / fabricat / processing / plant) — only when the description
    # doesn't already match anything else, fall back to generic mfg.
    (re.compile(r"\b(production\s+facility|production\s+plant|fabrication\s+(facility|plant)|stationary\s+plant|processing\s+plant)\b", re.I), "339999"),
    # ---------------- generic catch-all: any "manufactur*" wins ----------------
    (re.compile(r"\bmanufactur", re.I), "339999"),
]


def _is_mfg_code(code: str) -> bool:
    return code[:2] in ("31", "32", "33")


def classify_industry_to_naics(desc) -> Optional[str]:
    """Return a representative NAICS code derived from the description, or None.

    When a description matches both manufacturing and non-manufacturing rules
    (e.g. "portland cement manufacturing plant, associated limestone quarry"),
    prefer the manufacturing match.

    If no regex matches, fall back to the JSON cache populated by the cborg
    classification script.
    """
    if desc is None:
        return None
    if isinstance(desc, float) and pd.isna(desc):
        return None
    s = str(desc).strip()
    if not s:
        return None
    first_non_mfg: Optional[str] = None
    for pattern, code in _RULES:
        if pattern.search(s):
            if _is_mfg_code(code):
                return code
            if first_non_mfg is None:
                first_non_mfg = code
    if first_non_mfg is not None:
        return first_non_mfg
    cache = _load_cache()
    return cache.get(s.lower())


def is_manufacturing_industry_description(desc) -> bool:
    """True if the description maps to a NAICS in 31/32/33."""
    code = classify_industry_to_naics(desc)
    return code is not None and _is_mfg_code(code)
