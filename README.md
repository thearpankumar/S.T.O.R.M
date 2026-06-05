# Cybersec Research Agent

![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)
![AWS Bedrock](https://img.shields.io/badge/AWS_Bedrock-DeepSeek_v3.2-orange.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Status](https://img.shields.io/badge/Status-Active-brightgreen.svg)

An AI-powered research pipeline that automates cybersecurity tool discovery, feature extraction, and comparison matrix generation.

## Overview

The Cybersec Research Agent is an advanced analytical engine that systematically investigates 19 cybersecurity domains. It utilizes multi-agent LLM pipelines across five distinct research techniques to discover subdomains, rank industry tools, extract differentiating features, classify them against industry frameworks (NIST CSF 2.0), and score them on a unified multi-dimensional metric.

## Research Techniques

The agent employs five distinct pipelines to analyze the cybersecurity tooling landscape at varying depths.

### Technique 1: Subdomain Feature Matrix (M1–M5)
Focuses on granular analysis at the subdomain level. It discovers specific subdomains, identifies relevant tools, extracts granular features, and generates comprehensive Excel comparison matrices.

```mermaid
flowchart LR
    subgraph M1["M1: Discovery"]
        direction TB
        D1[("Domain")] --> WS1["Web Search"]
        WS1 --> L1["LLM + Consensus"]
        L1 --> SD[("Subdomains")]
    end

    subgraph M2["M2: Tooling"]
        direction TB
        SD2[("Subdomain")] --> WS2["Web Search"]
        WS2 --> L2["LLM"]
        L2 --> T[("Tools")]
    end

    subgraph M3["M3: Features"]
        direction TB
        T3[("Tools")] --> WS3["Web Search"]
        WS3 --> L3["LLM"]
        L3 --> F[("Features")]
    end

    subgraph M4["M4: Expansion"]
        direction TB
        F4[("Features")] --> L4["Parallel LLMs"]
        L4 --> SF[("Sub-features")]
    end

    subgraph M5["M5: Matrix"]
        direction TB
        IN["Tools + Sub-features"] --> L5["Batch LLM"]
        L5 --> MX[("Matrix")]
    end

    M1 --> M2 --> M3 --> M4 --> M5
    M5 --> EXCEL[("Excel Export")]
```

### Technique 2: Domain-Level Tool Rankings (D1–D5)
Operates at the broader domain level. It aggregates and ranks enterprise and open-source tools across an entire domain, generating high-level features and a unified domain matrix.

```mermaid
flowchart LR
    subgraph D1["D1: Aggregation & Ranking"]
        direction TB
        D[("Domain")] --> AGG["Tool Aggregation"]
        AGG --> RANK["Rank Enterprise & OSS"]
    end
    
    subgraph D2["D2: Domain Features"]
        direction TB
        T[("Ranked Tools")] --> FA["Feature Aggregation"]
    end
    
    subgraph D3["D3: Subfeatures"]
        direction TB
        FA --> SG["Generate Subfeatures"]
    end
    
    subgraph D4["D4: Matrix"]
        direction TB
        SG --> MP["Populate Matrix"]
    end
    
    D1 --> D2 --> D3 --> D4
    D4 --> EXCEL2[("Excel Export")]
```

### Technique 3: Cross-Domain Tool Classification (S1–S3)
Provides global insights by deduplicating tools across all domains and classifying them according to the NIST Cybersecurity Framework (Identify, Protect, Detect, Respond, Recover, Govern).

```mermaid
flowchart LR
    subgraph S1["S1: Deduplication"]
        direction TB
        DB[("T1 Tools DB")] --> DEDUP["SQL Deduplication"]
    end
    
    subgraph S2["S2: Classification"]
        direction TB
        DEDUP --> LLM["LLM NIST Classification"]
    end
    
    subgraph S3["S3: Synthesis"]
        direction TB
        LLM --> SUMM["Executive Summary"]
    end
    
    S1 --> S2 --> S3
    S3 --> EXCEL3[("Excel Export")]
    ```

### Technique 4: Tool-Level Cross-Domain Analysis (S1–S5)
Analyzes individual tools that appear across multiple domains/subdomains, detecting license models, aggregating feature support metrics, and calculating domain coverage.

```mermaid
flowchart LR
    subgraph S1["S1: Bootstrap"]
        direction TB
        T1[("T1 Tools")] --> DEDUP["SQL Deduplication"]
    end
    
    subgraph S2["S2: Enrichment"]
        direction TB
        DEDUP --> WEB["Web Search"]
        WEB --> LLM["LLM License Detection"]
    end
    
    subgraph S3["S3: Features"]
        direction TB
        MATRIX[("Matrix Cells")] --> AGG["Aggregate Support"]
    end
    
    subgraph S4["S4: Domains"]
        direction TB
        AGG --> DOMAINS["Domain Mapping"]
    end
    
    subgraph S5["S5: Excel"]
        direction TB
        DOMAINS --> EXCEL4[("Excel Dashboard")]
    end
    
    S1 --> S2 --> S3 --> S4 --> S5
```

### Technique 5: Strategic Score Card (S1–S5)
A synthesis layer that evaluates every canonical tool across five weighted dimensions (Feature Coverage, Domain Breadth, NIST Alignment, Market Maturity, Ranking Signal) to generate a unified readiness score (0-100) and letter grade.

```mermaid
flowchart LR
    subgraph S1["S1: Bootstrap"]
        direction TB
        T4[("T4 Canonical Tools")] --> JOIN["Join T2 & T3 Data"]
    end
    
    subgraph S2["S2: Score Compute"]
        direction TB
        JOIN --> DIM["Calculate D1-D5"]
    end
    
    subgraph S3["S3: Aggregate"]
        direction TB
        DIM --> COMP["Composite & Rank"]
        COMP --> DB[("T5 DB")]
    end
    
    subgraph S4["S4: LLM Insights"]
        direction TB
        DB --> LLM["Generate Strategic Summary"]
    end
    
    subgraph S5["S5: Excel"]
        direction TB
        LLM --> EXCEL5[("Score Card Dashboard")]
    end
    
    S1 --> S2 --> S3 --> S4 --> S5
```

## Architecture

```mermaid
flowchart LR
    subgraph CLI["Entry Point"]
        MAIN["main.py"]
    end

    subgraph ORCH["Orchestrator"]
        GRAPH["graph.py (T1)"]
        T2G["t2_graph.py"]
        T3G["t3_graph.py"]
        T4G["t4_graph.py"]
        T5G["t5_graph.py"]
    end

    subgraph AGENTS["Agents"]
        T1["T1: Discovery & Matrix Agents"]
        T2["T2: Domain Ranker"]
        T3["T3: NIST Classifier"]
        T4["T4: Web Enricher"]
        T5["T5: Scorer Engine"]
    end

    subgraph LLM["LLM Layer"]
        BEDROCK["bedrock.py"]
    end

    subgraph TOOLS["External Tools"]
        TAVILY["Tavily"]
        BRIGHT["BrightData"]
        FIRE["Firecrawl"]
    end

    subgraph DATA["Data Layer"]
        DB[("SQLite")]
        EXCEL[("Excel")]
    end

    MAIN --> CLI_MODE{"Mode?"}
    CLI_MODE -->|"Streamlit UI"| TUI["Streamlit Web App"]
    CLI_MODE -->|"CLI"| GRAPH

    ORCH --> AGENTS
    AGENTS --> BEDROCK
    T1 & T2 & T4 --> TOOLS
    TOOLS --> WEB[("Web")]
    ORCH --> EXCEL
    AGENTS --> DB
```

## Prerequisites

| Service | Purpose | Required |
|---------|---------|----------|
| AWS Bedrock | LLM inference (DeepSeek v3.2) | Yes |
| Tavily API | Primary web search | Yes |
| BrightData API | Fallback SERP | Optional |
| Firecrawl API | Web scraping | Optional |

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd cybersec-research-agent

# Install dependencies (using uv)
uv sync

# Or using pip
pip install -r requirements.txt
```

## Configuration

Create a `.env` file from the example:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region for Bedrock | `us-east-1` |
| `BEDROCK_MODEL_ID` | Model identifier | `deepseek.v3.2` |
| `TAVILY_API_KEY` | Tavily search API key | Required |
| `BRIGHTDATA_API_KEY` | BrightData SERP API key | Optional |
| `FIRECRAWL_API_KEY` | Firecrawl scraping API key | Optional |
| `DB_PATH` | SQLite database path | `data/agent.db` |
| `EXCEL_OUTPUT_PATH` | Output Excel file | `output/cybersec_matrix.xlsx` |
| `MAX_WORKERS` | Parallel subdomain pipelines | `3` |
| `LOG_DIR` | Log directory | `logs/` |
| `LOG_DISPLAY_LINES` | Lines to show in log panel | `100` |

## Usage

### Interactive Mode (Streamlit Web UI)

```bash
python main.py
# or explicitly
python main.py --mode streamlit
```

The Streamlit web interface provides:
- **Domain Explorer** - Navigate and manage cybersecurity domains/subdomains
- **Active Pipelines** - Monitor running research pipelines in real-time
- **Logs Panel** - View session logs with auto-refresh
- **Documentation** - Built-in README viewer with interactive Mermaid diagrams

#### Streamlit UI Features

| Feature | Description |
|---------|-------------|
| Domain Tree | Collapsible treeview with checkboxes for bulk operations |
| Detail Panel | Master-detail view for domains and subdomains |
| Pipeline Progress | Live progress bars with stage indicators (M2-M5) |
| Bulk Actions | Run, export, or clear multiple subdomains |
| Documentation Modal | View README with rendered Mermaid diagrams |

### Discover Subdomains

```bash
python main.py --mode discover --domain "Network Security"
```

### Process Single Subdomain

```bash
python main.py --mode single --domain "Network Security" --subdomain "Firewall Management"
```

### Batch Processing

```bash
python main.py --mode batch --domains "Network Security" "Cloud Security" "DevSecOps"
```

## Supported Domains

The agent covers 19 cybersecurity domains:

| | | | |
|---|---|---|---|
| Network Security | Application Security | Cloud Security | Endpoint Security |
| DevSecOps | Identity & Access Management | GRC | Security Operations (SOC) |
| Threat Intelligence | Malware Analysis | Incident Response | OT/ICS Security |
| Mobile Security | AI Security | Cryptography | Information Security |
| Cyber Defense | Digital Forensics | Offensive Security | |

## Output

The pipeline generates distinct Excel reports for each research technique, driven by a unified local database:

- **`data/agent.db`** - Comprehensive SQLite database storing all discovered entities, rankings, and cross-domain classifications.
- **`output/cybersec_matrix.xlsx`** (Technique 1) - Detailed workbook containing tool-feature matrices per subdomain, with support levels: ✔ (Full), Partial, ✘ (None).
- **`output/technique2_domain_rankings.xlsx`** (Technique 2) - Domain-level tool rankings and broader feature comparisons.
- **`output/technique3_tool_classification.xlsx`** (Technique 3) - Global cross-domain tool classification mapping (NIST CSF 2.0), interactive dashboard, and executive summary.
- **`output/technique4_tool_analysis.xlsx`** (Technique 4) - Tool-level cross-domain analysis with license detection, feature support metrics, and domain coverage dashboards.
- **`output/technique5_scorecard.xlsx`** (Technique 5) - Strategic Tool Score Card featuring head-to-head dimension comparisons, LLM-generated strategic insights, and multi-dimensional readiness scores.

## License

MIT License
