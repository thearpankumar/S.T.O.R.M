SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subdomains (
    id INTEGER PRIMARY KEY,
    domain_id INTEGER REFERENCES domains(id),
    name TEXT NOT NULL,
    status TEXT DEFAULT 'pending' 
        CHECK(status IN ('pending', 'running', 'done', 'failed')),
    confidence_score REAL,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain_id, name)
);

CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY,
    subdomain_id INTEGER REFERENCES subdomains(id),
    vendor TEXT NOT NULL,
    product_name TEXT NOT NULL,
    tool_type TEXT CHECK(tool_type IN ('enterprise', 'opensource')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subdomain_id, product_name)
);

CREATE TABLE IF NOT EXISTS features (
    id INTEGER PRIMARY KEY,
    subdomain_id INTEGER REFERENCES subdomains(id),
    name TEXT NOT NULL,
    rank_order INTEGER,
    UNIQUE(subdomain_id, name)
);

CREATE TABLE IF NOT EXISTS subfeatures (
    id INTEGER PRIMARY KEY,
    feature_id INTEGER REFERENCES features(id),
    name TEXT NOT NULL,
    rank_order INTEGER,
    UNIQUE(feature_id, name)
);

CREATE TABLE IF NOT EXISTS matrix_cells (
    id INTEGER PRIMARY KEY,
    subdomain_id INTEGER REFERENCES subdomains(id),
    subfeature_id INTEGER REFERENCES subfeatures(id),
    tool_id INTEGER REFERENCES tools(id),
    support_level TEXT CHECK(support_level IN ('✔', '✘', 'Partial')),
    UNIQUE(subdomain_id, subfeature_id, tool_id)
);

CREATE TABLE IF NOT EXISTS worker_state (
    subdomain_id INTEGER PRIMARY KEY REFERENCES subdomains(id),
    state_json TEXT NOT NULL,
    current_step TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tool_quota (
    tool_name TEXT PRIMARY KEY,
    quota_remaining INTEGER,
    exhausted BOOLEAN DEFAULT FALSE,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS excel_row_map (
    subdomain TEXT PRIMARY KEY,
    sheet_name TEXT,
    start_row INTEGER,
    end_row INTEGER,
    last_written TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_subdomains_domain ON subdomains(domain_id);
CREATE INDEX IF NOT EXISTS idx_subdomains_status ON subdomains(status);
CREATE INDEX IF NOT EXISTS idx_tools_subdomain ON tools(subdomain_id);
CREATE INDEX IF NOT EXISTS idx_features_subdomain ON features(subdomain_id);
CREATE INDEX IF NOT EXISTS idx_subfeatures_feature ON subfeatures(feature_id);
CREATE INDEX IF NOT EXISTS idx_matrix_subdomain ON matrix_cells(subdomain_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- TECHNIQUE 2: Domain-level tool ranking and comparison
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t2_domain_rankings (
    id INTEGER PRIMARY KEY,
    domain_id INTEGER REFERENCES domains(id),
    status TEXT DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'failed')),
    total_enterprise_tools INTEGER DEFAULT 0,
    total_opensource_tools INTEGER DEFAULT 0,
    selected_enterprise_tools INTEGER DEFAULT 0,
    selected_opensource_tools INTEGER DEFAULT 0,
    total_features INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain_id)
);

CREATE TABLE IF NOT EXISTS t2_domain_tools (
    id INTEGER PRIMARY KEY,
    domain_id INTEGER REFERENCES domains(id),
    vendor TEXT NOT NULL,
    product_name TEXT NOT NULL,
    tool_type TEXT CHECK(tool_type IN ('enterprise', 'opensource')),
    rank_position INTEGER,
    composite_score REAL,
    subdomain_presence_count INTEGER DEFAULT 0,
    subdomain_presence_score REAL DEFAULT 0.0,
    feature_coverage_score REAL DEFAULT 0.0,
    market_presence_score REAL DEFAULT 0.0,
    rank_distribution_score REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain_id, product_name)
);

CREATE TABLE IF NOT EXISTS t2_domain_features (
    id INTEGER PRIMARY KEY,
    domain_id INTEGER REFERENCES domains(id),
    name TEXT NOT NULL,
    rank_order INTEGER,
    source_subdomains TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain_id, name)
);

CREATE TABLE IF NOT EXISTS t2_domain_subfeatures (
    id INTEGER PRIMARY KEY,
    domain_feature_id INTEGER REFERENCES t2_domain_features(id),
    name TEXT NOT NULL,
    rank_order INTEGER,
    UNIQUE(domain_feature_id, name)
);

CREATE TABLE IF NOT EXISTS t2_domain_matrix_cells (
    id INTEGER PRIMARY KEY,
    domain_id INTEGER REFERENCES domains(id),
    domain_subfeature_id INTEGER REFERENCES t2_domain_subfeatures(id),
    domain_tool_id INTEGER REFERENCES t2_domain_tools(id),
    support_level TEXT CHECK(support_level IN ('✔', '✘', 'Partial')),
    UNIQUE(domain_id, domain_subfeature_id, domain_tool_id)
);

CREATE TABLE IF NOT EXISTS t2_domain_worker_state (
    domain_id INTEGER PRIMARY KEY REFERENCES domains(id),
    state_json TEXT NOT NULL,
    current_step TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Technique 2 indexes
CREATE INDEX IF NOT EXISTS idx_t2_domain_tools_domain ON t2_domain_tools(domain_id);
CREATE INDEX IF NOT EXISTS idx_t2_domain_tools_rank ON t2_domain_tools(domain_id, rank_position);
CREATE INDEX IF NOT EXISTS idx_t2_domain_features_domain ON t2_domain_features(domain_id);
CREATE INDEX IF NOT EXISTS idx_t2_domain_subfeatures_feature ON t2_domain_subfeatures(domain_feature_id);
CREATE INDEX IF NOT EXISTS idx_t2_domain_matrix_domain ON t2_domain_matrix_cells(domain_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- TECHNIQUE 2: Subdomain-level tool ranking
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t2_subdomain_rankings (
    id INTEGER PRIMARY KEY,
    subdomain_id INTEGER UNIQUE REFERENCES subdomains(id),
    status TEXT DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'failed')),
    ranked_enterprise_tools INTEGER DEFAULT 0,
    ranked_opensource_tools INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS t2_subdomain_tools (
    id INTEGER PRIMARY KEY,
    subdomain_id INTEGER REFERENCES subdomains(id),
    tool_id INTEGER REFERENCES tools(id),
    vendor TEXT NOT NULL,
    product_name TEXT NOT NULL,
    tool_type TEXT CHECK(tool_type IN ('enterprise', 'opensource')),
    rank_position INTEGER,
    composite_score REAL,
    feature_coverage_score REAL DEFAULT 0.0,
    market_presence_score REAL DEFAULT 0.0,
    rank_distribution_score REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subdomain_id, product_name)
);

CREATE TABLE IF NOT EXISTS t2_subdomain_worker_state (
    subdomain_id INTEGER PRIMARY KEY REFERENCES subdomains(id),
    state_json TEXT NOT NULL,
    current_step TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_t2_subdomain_tools_subdomain ON t2_subdomain_tools(subdomain_id);
CREATE INDEX IF NOT EXISTS idx_t2_subdomain_tools_rank ON t2_subdomain_tools(subdomain_id, rank_position);
CREATE INDEX IF NOT EXISTS idx_t2_subdomain_rankings_status ON t2_subdomain_rankings(status);
"""
