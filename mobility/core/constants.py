"""Stage-scoped core constants shared across mobility modules."""

STEP_MIN_DECISION = 15
STEP_MIN_PROFILE = 30
STEPS_PER_DAY_DECISION = 96
STEPS_PER_DAY_PROFILE = 48
STEP_HOURS_DECISION = 0.25
STEP_HOURS_PROFILE = 0.5
SOC_SAFETY_MARGIN = 0.05

SCENE_CATEGORIES = [
    "work",
    "education",
    "shopping",
    "personal_business",
    "social",
    "leisure",
    "holiday",
]

HUFF_LAYER1_DMIN_M = 500.0
HUFF_LAYER1_DSCALE_KM = 20.0
HUFF_LAYER1_BETA = 1.5
HUFF_LAYER1_TOPK = 500

HUFF_LAYER2_DMIN_M = 50.0
HUFF_LAYER2_DSCALE_M = 500.0
HUFF_LAYER2_BETA = 1.5

WARMUP_DAYS = 14

# Home charging (Stage 6)
HOME_CHARGER_KW = 7.0

# AC charging curve (Stage 4)
CV_THRESHOLD = {"NMC": 0.80, "LFP": 0.88}
DEFAULT_CHEMISTRY = "NMC"

# Seasonal consumption correction (Stage 5)
SEASONAL_CONSUMPTION_FACTOR = {
    "winter": 1.35,  # 12 / 1 / 2 — heating load
    "spring": 1.00,  # 3 / 4 / 5
    "summer": 1.10,  # 6 / 7 / 8 — AC load
    "autumn": 1.00,  # 9 / 10 / 11
}
MONTH_TO_SEASON = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}
