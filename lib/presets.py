"""Preset metadata for known RealPage repos.

Sourced from RealRAG-scope.html (per-repo overview + indexing strategy).
Pre-fills everything except `tfs_url` and `pat`, which are user-specific.
"""

from __future__ import annotations

from typing import TypedDict


class RepoPreset(TypedDict):
    product_name: str
    display_name: str
    repo_role: str
    priority: str
    branch: str
    one_line_description: str
    major_workflows: list[str]
    key_business_concepts: list[str]
    critical_entry_points: list[str]
    languages: list[str]
    owner_team: str
    owner_contact: str
    special_notes: str
    clone_depth: str


RMS_PRESETS: dict[str, RepoPreset] = {
    "po": {
        "product_name": "RMS",
        "display_name": "po",
        "repo_role": "ETL",
        "priority": "P0",
        "branch": "master",
        "one_line_description": (
            "Java backend with the calculation engine — runs all RMS ETLs and "
            "computes recommended rents per unit. Heart of the application."
        ),
        "major_workflows": [
            "TOYSMaster ETL",
            "TOPOETL",
            "Forecasting",
            "Rates Generation",
            "Seasonality",
            "Stabilization",
            "Operating Statistics",
            "Demand",
            "Surrogate Finder",
            "Yoda — property onboarding",
        ],
        "key_business_concepts": [
            "propcode",
            "fpcode",
            "useAdjustedRent",
            "recommended rent",
            "seasonal forecast adjustment",
            "renewal rate",
            "stabilization date",
            "operating statistics",
            "demand model",
            "surrogate finder",
        ],
        "critical_entry_points": [
            "EtlToPOExecutor.java",
            "UnitRatesGenerator.java",
            "ForecastShell.java",
            "ForecastPhase1.java",
            "etl2posql.xml",
        ],
        "languages": ["Java", "XML", "SQL"],
        "owner_team": "RMS Core",
        "owner_contact": "rms-core@realpage.com",
        "special_notes": "Exclude rmsws-webapp/ — legacy.",
        "clone_depth": "shallow_50",
    },
    "ys": {
        "product_name": "RMS",
        "display_name": "ys",
        "repo_role": "API",
        "priority": "P0",
        "branch": "master",
        "one_line_description": (
            "Java REST APIs and named-query catalogs — bridge layer between "
            "rm-web UI and client databases."
        ),
        "major_workflows": [
            "Pricing Review",
            "Property + Portfolio APIs",
            "Named-query dispatch",
            "AIRM queries",
        ],
        "key_business_concepts": [
            "named queries",
            "yscore_queries",
            "airm_queries",
            "PricingReview",
        ],
        "critical_entry_points": [
            "ys-core/src/main/resources/queries/yscore_queries.xml",
            "ys-core/src/main/resources/queries/airm_queries.xml",
            "PricingReviewController.java",
            "PricingReviewServiceImpl.java",
            "NamedQueries.getNamedQuery",
        ],
        "languages": ["Java", "XML", "SQL"],
        "owner_team": "ys",
        "owner_contact": "ys-team@realpage.com",
        "special_notes": "",
        "clone_depth": "shallow_50",
    },
    "rm-web": {
        "product_name": "RMS",
        "display_name": "rm-web",
        "repo_role": "UI",
        "priority": "P0",
        "branch": "master",
        "one_line_description": (
            "Angular UI — property + portfolio dashboards plus all RMS "
            "workflows (RRG, RRUL, leasing, renewal, lease-up, ESS)."
        ),
        "major_workflows": [
            "Property Dashboard",
            "Portfolio Dashboards",
            "RRG (Rent Roll Grid)",
            "RRUL (Rent Roll Unit-Level)",
            "Leasing Workflow",
            "Renewal Expirations / Renewal Rates",
            "Lease-Up Analysis",
            "ESS (Expiration Summary Statistics)",
            "Unit Management",
            "Settings",
        ],
        "key_business_concepts": [
            "widget",
            "workflow",
            "RRG",
            "RRUL",
            "ESS",
            "useAdjustedRent flag",
        ],
        "critical_entry_points": [
            "src/app/dashboard/",
            "src/app/rrg/",
            "src/app/rrul/",
            "src/app/leasingWorkflow/",
            "src/app/renewal-expirations/",
            "docs/workflow_map.yaml",
        ],
        "languages": ["TypeScript", "HTML", "SCSS"],
        "owner_team": "rm-web",
        "owner_contact": "rm-web@realpage.com",
        "special_notes": "",
        "clone_depth": "shallow_50",
    },
    "ys-reports": {
        "product_name": "RMS",
        "display_name": "ys-reports",
        "repo_role": "Reports",
        "priority": "P0",
        "branch": "master",
        "one_line_description": (
            "JASPER reporting service — 200+ JRXML reports compiled and "
            "served to rm-web."
        ),
        "major_workflows": [
            "Amenity reports",
            "Auto-acceptance reports",
            "Days On Market",
            "Property detail reports",
            "Scheduled report runs",
        ],
        "key_business_concepts": [
            "JRXML",
            "JASPER",
            "report parameters",
            "sub-report",
            "report scheduler",
        ],
        "critical_entry_points": [
            "ys-reports-core/src/main/resources/reports/jasper/",
            "ys-reports-api",
            "ys-reports-scheduler",
        ],
        "languages": ["Java", "XML"],
        "owner_team": "ys-reports",
        "owner_contact": "ys-reports@realpage.com",
        "special_notes": "",
        "clone_depth": "shallow_50",
    },
    "ys-admin": {
        "product_name": "RMS",
        "display_name": "ys-admin",
        "repo_role": "Config",
        "priority": "P1",
        "branch": "master",
        "one_line_description": (
            "Java + UI for managing ETLs, properties, servers — writes to "
            "poconfig DB which po reads at startup."
        ),
        "major_workflows": [
            "ETL configuration",
            "Property onboarding",
            "poconfig management",
            "polog inspection",
        ],
        "key_business_concepts": [
            "poconfig",
            "polog",
            "ETL recipe",
            "property onboarding",
        ],
        "critical_entry_points": [
            "database/POConfigSchema.sql",
            "database/POLogSchema.sql",
            "ys-admin-services",
            "ys-admin-web",
        ],
        "languages": ["Java"],
        "owner_team": "ys-admin",
        "owner_contact": "ys-admin@realpage.com",
        "special_notes": "",
        "clone_depth": "shallow_50",
    },
}
