-- Schema snapshot for the MoneyGraph public example repo.
-- Generated via: docker exec moneygraph-db-1 pg_dump -U postgres -d aigraph --schema-only --no-owner --no-privileges
-- Reflects the current real schema (supersedes db/migrations/ — those are folded in here, not replayed).

--
-- Name: alias_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.alias_source AS ENUM (
    'seed',
    'user_approved',
    'pipeline_auto'
);


--
-- Name: candidate_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.candidate_status AS ENUM (
    'pending',
    'approved',
    'rejected'
);


--
-- Name: confidence_level; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.confidence_level AS ENUM (
    'high',
    'medium',
    'low'
);


--
-- Name: dark_horse_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dark_horse_status AS ENUM (
    'watching',
    'graduated',
    'dismissed'
);


--
-- Name: edge_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.edge_status AS ENUM (
    'active',
    'partial_exit',
    'exited',
    'cancelled'
);


--
-- Name: event_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.event_type AS ENUM (
    'investment',
    'partial_exit',
    'full_exit',
    'cancelled',
    'correction'
);


--
-- Name: node_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.node_status AS ENUM (
    'active',
    'watchlist',
    'graduated'
);


--
-- Name: node_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.node_type AS ENUM (
    'public',
    'private',
    'dark_horse'
);


--
-- Name: pipeline_run_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.pipeline_run_status AS ENUM (
    'running',
    'completed',
    'failed'
);


--
-- Name: enforce_edge_has_source(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_edge_has_source() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sources WHERE edge_id = NEW.id) THEN
        RAISE EXCEPTION 'edge % has no backing source row', NEW.id;
    END IF;
    RETURN NULL;
END;
$$;


--
-- Name: utc_date(timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.utc_date(ts timestamp with time zone) RETURNS date
    LANGUAGE sql IMMUTABLE
    AS $$ SELECT (ts AT TIME ZONE 'UTC')::date $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.candidates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    discovered_via text NOT NULL,
    suggested_investor uuid,
    amount_usd bigint,
    status public.candidate_status DEFAULT 'pending'::public.candidate_status NOT NULL,
    discovered_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone,
    notes text,
    normalized_name text NOT NULL,
    discovered_urls text[] DEFAULT '{}'::text[] NOT NULL,
    discovered_by_nodes uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    discovery_count integer DEFAULT 1 NOT NULL,
    facts jsonb
);


--
-- Name: dark_horses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dark_horses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    investor_count integer DEFAULT 0 NOT NULL,
    investors uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    total_known_investment bigint DEFAULT 0 NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    threshold_at_discovery integer NOT NULL,
    status public.dark_horse_status DEFAULT 'watching'::public.dark_horse_status NOT NULL
);


--
-- Name: edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    from_node_id uuid NOT NULL,
    to_node_id uuid NOT NULL,
    net_amount_usd bigint DEFAULT 0 NOT NULL,
    first_seen timestamp with time zone NOT NULL,
    last_confirmed timestamp with time zone NOT NULL,
    source_count integer DEFAULT 0 NOT NULL,
    is_confirmed boolean DEFAULT false NOT NULL,
    status public.edge_status DEFAULT 'active'::public.edge_status NOT NULL,
    meta jsonb,
    edge_type text DEFAULT 'ownership'::text NOT NULL,
    CONSTRAINT edges_edge_type_check CHECK ((edge_type = ANY (ARRAY['ownership'::text, 'subsidiary'::text, 'joint_venture'::text, 'creditor_debtor'::text, 'supplier_customer'::text])))
);


--
-- Name: investment_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.investment_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    edge_id uuid NOT NULL,
    delta_usd bigint NOT NULL,
    event_type public.event_type NOT NULL,
    event_date date NOT NULL,
    source_url text NOT NULL,
    source_tier integer NOT NULL,
    filing_type text,
    confidence public.confidence_level NOT NULL,
    raw_excerpt text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    verification_status text DEFAULT 'extracted'::text NOT NULL,
    value_status text DEFAULT 'actual'::text NOT NULL,
    discovery_source text DEFAULT 'edgar'::text NOT NULL,
    estimate_reason text,
    corrects_event_id uuid,
    CONSTRAINT investment_events_discovery_source_check CHECK ((discovery_source = ANY (ARRAY['edgar'::text, 'web'::text]))),
    CONSTRAINT investment_events_estimate_reason_check CHECK ((estimate_reason = ANY (ARRAY['no_amount'::text, 'syndicate_total'::text, 'manual_correction'::text]))),
    CONSTRAINT investment_events_source_tier_check CHECK (((source_tier >= 1) AND (source_tier <= 5))),
    CONSTRAINT investment_events_value_status_check CHECK ((value_status = ANY (ARRAY['actual'::text, 'estimated'::text]))),
    CONSTRAINT investment_events_verification_status_check CHECK ((verification_status = ANY (ARRAY['extracted'::text, 'corroborated'::text, 'verified'::text])))
);


--
-- Name: news_feed; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_feed (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    headline text NOT NULL,
    url text NOT NULL,
    source_tier integer NOT NULL,
    source_name text NOT NULL,
    published_at timestamp with time zone NOT NULL,
    extracted_investor text,
    extracted_investee text,
    amount_usd bigint,
    confirmed_by_sec boolean DEFAULT false NOT NULL,
    sec_source_id uuid,
    pipeline_run_id uuid NOT NULL,
    normalized_investor text,
    normalized_investee text,
    CONSTRAINT news_feed_source_tier_check CHECK (((source_tier >= 1) AND (source_tier <= 5)))
);


--
-- Name: node_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_aliases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    node_id uuid NOT NULL,
    alias text NOT NULL,
    normalized_alias text NOT NULL,
    source public.alias_source NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: node_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_facts (
    node_id uuid NOT NULL,
    is_public boolean,
    founded integer,
    sector text,
    headquarters text,
    short_description text,
    wikidata_qid text,
    source text,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    country text,
    CONSTRAINT node_facts_source_check CHECK ((source = ANY (ARRAY['wikidata'::text, 'edgar'::text, 'both'::text])))
);


--
-- Name: node_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_metrics (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    node_id uuid NOT NULL,
    metric text NOT NULL,
    period_end date NOT NULL,
    value numeric NOT NULL,
    unit text,
    source_url text,
    extracted_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: node_tickers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_tickers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    node_id uuid NOT NULL,
    exchange text DEFAULT ''::text NOT NULL,
    ticker text NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    active boolean DEFAULT true NOT NULL,
    delisted_at timestamp with time zone
);


--
-- Name: nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nodes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    ticker text,
    type public.node_type NOT NULL,
    cik text,
    status public.node_status DEFAULT 'active'::public.node_status NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    added_by text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_websearched_at timestamp with time zone
);


--
-- Name: openai_response_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.openai_response_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    endpoint text NOT NULL,
    model text NOT NULL,
    response_id text NOT NULL,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: pipeline_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipeline_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    status public.pipeline_run_status DEFAULT 'running'::public.pipeline_run_status NOT NULL,
    nodes_processed integer DEFAULT 0 NOT NULL,
    edges_created integer DEFAULT 0 NOT NULL,
    candidates_found integer DEFAULT 0 NOT NULL,
    events_logged integer DEFAULT 0 NOT NULL,
    error_message text,
    extraction_mode text DEFAULT 'realtime'::text NOT NULL,
    batch_id text,
    awaiting_harvest_since timestamp with time zone,
    run_type text DEFAULT 'edgar'::text NOT NULL,
    search_calls_made integer DEFAULT 0 NOT NULL,
    total_units integer,
    units_processed integer DEFAULT 0 NOT NULL,
    CONSTRAINT pipeline_runs_extraction_mode_check CHECK ((extraction_mode = ANY (ARRAY['realtime'::text, 'batch'::text]))),
    CONSTRAINT pipeline_runs_run_type_check CHECK ((run_type = ANY (ARRAY['edgar'::text, 'rss'::text, 'websearch'::text, 'legacy'::text, 'reresolve'::text])))
);


--
-- Name: processed_filings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processed_filings (
    cik text NOT NULL,
    accession text NOT NULL,
    content_hash text NOT NULL,
    run_id uuid,
    events_count integer DEFAULT 0 NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: processed_web_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processed_web_sources (
    url text NOT NULL,
    content_hash text NOT NULL,
    run_id uuid,
    events_count integer DEFAULT 0 NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: processing_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processing_batches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    batch_id text NOT NULL,
    custom_id text NOT NULL,
    cik text NOT NULL,
    accession text NOT NULL,
    form_type text NOT NULL,
    filing_date text,
    source_url text,
    prompt_version text NOT NULL,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    harvested_at timestamp with time zone
);


--
-- Name: settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.settings (
    key text NOT NULL,
    value text NOT NULL,
    description text NOT NULL,
    default_value text NOT NULL
);


--
-- Name: snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snapshots (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    captured_at timestamp with time zone DEFAULT now() NOT NULL,
    node_count integer NOT NULL,
    edge_count integer NOT NULL,
    graph_json jsonb NOT NULL,
    delta_json jsonb
);


--
-- Name: sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sources (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    edge_id uuid NOT NULL,
    url text NOT NULL,
    filing_type text,
    source_tier integer NOT NULL,
    published_at timestamp with time zone NOT NULL,
    parsed_at timestamp with time zone DEFAULT now() NOT NULL,
    raw_excerpt text NOT NULL,
    event_id uuid,
    discovery_source text DEFAULT 'edgar'::text NOT NULL,
    CONSTRAINT sources_discovery_source_check CHECK ((discovery_source = ANY (ARRAY['edgar'::text, 'web'::text]))),
    CONSTRAINT sources_source_tier_check CHECK (((source_tier >= 1) AND (source_tier <= 5)))
);


--
-- Name: stock_price_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stock_price_cache (
    ticker text NOT NULL,
    bucket text NOT NULL,
    data jsonb NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT stock_price_cache_bucket_check CHECK ((bucket = ANY (ARRAY['1d'::text, '1m'::text, '1y'::text, '5y'::text, 'max'::text])))
);


--
-- Name: candidates candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_pkey PRIMARY KEY (id);


--
-- Name: dark_horses dark_horses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dark_horses
    ADD CONSTRAINT dark_horses_pkey PRIMARY KEY (id);


--
-- Name: edges edges_from_node_id_to_node_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_from_node_id_to_node_id_key UNIQUE (from_node_id, to_node_id);


--
-- Name: edges edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_pkey PRIMARY KEY (id);


--
-- Name: investment_events investment_events_canonical_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investment_events
    ADD CONSTRAINT investment_events_canonical_key UNIQUE (edge_id, event_type, source_url, delta_usd);


--
-- Name: investment_events investment_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investment_events
    ADD CONSTRAINT investment_events_pkey PRIMARY KEY (id);


--
-- Name: news_feed news_feed_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_feed
    ADD CONSTRAINT news_feed_pkey PRIMARY KEY (id);


--
-- Name: node_aliases node_aliases_normalized_alias_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_aliases
    ADD CONSTRAINT node_aliases_normalized_alias_key UNIQUE (normalized_alias);


--
-- Name: node_aliases node_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_aliases
    ADD CONSTRAINT node_aliases_pkey PRIMARY KEY (id);


--
-- Name: node_facts node_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_facts
    ADD CONSTRAINT node_facts_pkey PRIMARY KEY (node_id);


--
-- Name: node_metrics node_metrics_node_id_metric_period_end_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_metrics
    ADD CONSTRAINT node_metrics_node_id_metric_period_end_key UNIQUE (node_id, metric, period_end);


--
-- Name: node_metrics node_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_metrics
    ADD CONSTRAINT node_metrics_pkey PRIMARY KEY (id);


--
-- Name: node_tickers node_tickers_node_id_exchange_ticker_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_tickers
    ADD CONSTRAINT node_tickers_node_id_exchange_ticker_key UNIQUE (node_id, exchange, ticker);


--
-- Name: node_tickers node_tickers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_tickers
    ADD CONSTRAINT node_tickers_pkey PRIMARY KEY (id);


--
-- Name: nodes nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_pkey PRIMARY KEY (id);


--
-- Name: openai_response_log openai_response_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.openai_response_log
    ADD CONSTRAINT openai_response_log_pkey PRIMARY KEY (id);


--
-- Name: pipeline_runs pipeline_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_runs
    ADD CONSTRAINT pipeline_runs_pkey PRIMARY KEY (id);


--
-- Name: processed_filings processed_filings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_filings
    ADD CONSTRAINT processed_filings_pkey PRIMARY KEY (cik, accession);


--
-- Name: processed_web_sources processed_web_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_web_sources
    ADD CONSTRAINT processed_web_sources_pkey PRIMARY KEY (url);


--
-- Name: processing_batches processing_batches_batch_id_custom_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processing_batches
    ADD CONSTRAINT processing_batches_batch_id_custom_id_key UNIQUE (batch_id, custom_id);


--
-- Name: processing_batches processing_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processing_batches
    ADD CONSTRAINT processing_batches_pkey PRIMARY KEY (id);


--
-- Name: settings settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settings
    ADD CONSTRAINT settings_pkey PRIMARY KEY (key);


--
-- Name: snapshots snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snapshots
    ADD CONSTRAINT snapshots_pkey PRIMARY KEY (id);


--
-- Name: sources sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);


--
-- Name: stock_price_cache stock_price_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stock_price_cache
    ADD CONSTRAINT stock_price_cache_pkey PRIMARY KEY (ticker, bucket);


--
-- Name: candidates_pending_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX candidates_pending_unique ON public.candidates USING btree (normalized_name) WHERE (status = 'pending'::public.candidate_status);


--
-- Name: idx_edges_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_edges_from ON public.edges USING btree (from_node_id);


--
-- Name: idx_edges_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_edges_to ON public.edges USING btree (to_node_id);


--
-- Name: idx_inv_events_corrects; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_events_corrects ON public.investment_events USING btree (corrects_event_id) WHERE (corrects_event_id IS NOT NULL);


--
-- Name: idx_inv_events_edge; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_events_edge ON public.investment_events USING btree (edge_id);


--
-- Name: idx_news_pipeline_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_pipeline_run ON public.news_feed USING btree (pipeline_run_id);


--
-- Name: idx_news_published_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_published_at ON public.news_feed USING btree (published_at DESC);


--
-- Name: idx_node_aliases_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_node_aliases_norm ON public.node_aliases USING btree (normalized_alias);


--
-- Name: idx_node_metrics_node_metric; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_node_metrics_node_metric ON public.node_metrics USING btree (node_id, metric, period_end DESC);


--
-- Name: idx_node_tickers_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_node_tickers_active ON public.node_tickers USING btree (node_id) WHERE active;


--
-- Name: idx_node_tickers_node; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_node_tickers_node ON public.node_tickers USING btree (node_id);


--
-- Name: idx_node_tickers_one_primary; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_node_tickers_one_primary ON public.node_tickers USING btree (node_id) WHERE is_primary;


--
-- Name: idx_nodes_ticker; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_nodes_ticker ON public.nodes USING btree (ticker) WHERE (ticker IS NOT NULL);


--
-- Name: idx_nodes_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_nodes_type ON public.nodes USING btree (type);


--
-- Name: idx_openai_response_log_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_openai_response_log_created ON public.openai_response_log USING btree (created_at DESC);


--
-- Name: idx_openai_response_log_response_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_openai_response_log_response_id ON public.openai_response_log USING btree (response_id);


--
-- Name: idx_processed_filings_processed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_processed_filings_processed_at ON public.processed_filings USING btree (processed_at DESC);


--
-- Name: idx_processed_filings_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_processed_filings_run ON public.processed_filings USING btree (run_id);


--
-- Name: idx_processed_web_sources_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_processed_web_sources_run ON public.processed_web_sources USING btree (run_id);


--
-- Name: idx_processing_batches_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_processing_batches_run ON public.processing_batches USING btree (run_id, harvested_at);


--
-- Name: idx_sources_edge; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_edge ON public.sources USING btree (edge_id);


--
-- Name: news_feed_canonical_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX news_feed_canonical_key ON public.news_feed USING btree (normalized_investor, normalized_investee, public.utc_date(published_at), amount_usd);


--
-- Name: edges edges_require_source; Type: TRIGGER; Schema: public; Owner: -
--

CREATE CONSTRAINT TRIGGER edges_require_source AFTER INSERT OR UPDATE ON public.edges DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.enforce_edge_has_source();


--
-- Name: candidates candidates_suggested_investor_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_suggested_investor_fkey FOREIGN KEY (suggested_investor) REFERENCES public.nodes(id);


--
-- Name: edges edges_from_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_from_node_id_fkey FOREIGN KEY (from_node_id) REFERENCES public.nodes(id);


--
-- Name: edges edges_to_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edges
    ADD CONSTRAINT edges_to_node_id_fkey FOREIGN KEY (to_node_id) REFERENCES public.nodes(id);


--
-- Name: investment_events investment_events_corrects_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investment_events
    ADD CONSTRAINT investment_events_corrects_event_id_fkey FOREIGN KEY (corrects_event_id) REFERENCES public.investment_events(id);


--
-- Name: investment_events investment_events_edge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investment_events
    ADD CONSTRAINT investment_events_edge_id_fkey FOREIGN KEY (edge_id) REFERENCES public.edges(id);


--
-- Name: news_feed news_feed_pipeline_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_feed
    ADD CONSTRAINT news_feed_pipeline_run_id_fkey FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_runs(id);


--
-- Name: news_feed news_feed_sec_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_feed
    ADD CONSTRAINT news_feed_sec_source_id_fkey FOREIGN KEY (sec_source_id) REFERENCES public.sources(id);


--
-- Name: node_aliases node_aliases_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_aliases
    ADD CONSTRAINT node_aliases_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.nodes(id) ON DELETE CASCADE;


--
-- Name: node_facts node_facts_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_facts
    ADD CONSTRAINT node_facts_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.nodes(id) ON DELETE CASCADE;


--
-- Name: node_metrics node_metrics_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_metrics
    ADD CONSTRAINT node_metrics_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.nodes(id) ON DELETE CASCADE;


--
-- Name: node_tickers node_tickers_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_tickers
    ADD CONSTRAINT node_tickers_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.nodes(id) ON DELETE CASCADE;


--
-- Name: processed_filings processed_filings_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_filings
    ADD CONSTRAINT processed_filings_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.pipeline_runs(id);


--
-- Name: processed_web_sources processed_web_sources_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_web_sources
    ADD CONSTRAINT processed_web_sources_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.pipeline_runs(id);


--
-- Name: processing_batches processing_batches_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processing_batches
    ADD CONSTRAINT processing_batches_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.pipeline_runs(id);


--
-- Name: sources sources_edge_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_edge_id_fkey FOREIGN KEY (edge_id) REFERENCES public.edges(id);


--
-- Name: sources sources_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.investment_events(id);


--
-- PostgreSQL database dump complete
--

