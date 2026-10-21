CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE audit_logs (
	tenant_id VARCHAR NOT NULL, 
	actor_id VARCHAR(64) NOT NULL, 
	action VARCHAR(64) NOT NULL, 
	resource_type VARCHAR(64) NOT NULL, 
	resource_id VARCHAR(128) NOT NULL, 
	detail JSONB, 
	trace_id VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id UUID NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_audit_actor ON audit_logs (tenant_id, actor_id, created_at);
CREATE INDEX ix_audit_logs_tenant_id ON audit_logs (tenant_id);
CREATE INDEX ix_audit_resource ON audit_logs (resource_type, resource_id);

CREATE TABLE buyer_profiles (
	tenant_id VARCHAR NOT NULL, 
	buyer_id VARCHAR(64) NOT NULL, 
	region VARCHAR(32) NOT NULL, 
	age_group VARCHAR(16) NOT NULL, 
	repeat_level VARCHAR(16) NOT NULL, 
	aov_band VARCHAR(16) NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_profiles_tenant_buyer UNIQUE (tenant_id, buyer_id)
)

;
CREATE INDEX ix_buyer_profiles_age_group ON buyer_profiles (age_group);
CREATE INDEX ix_buyer_profiles_aov_band ON buyer_profiles (aov_band);
CREATE INDEX ix_buyer_profiles_buyer_id ON buyer_profiles (buyer_id);
CREATE INDEX ix_buyer_profiles_region ON buyer_profiles (region);
CREATE INDEX ix_buyer_profiles_repeat_level ON buyer_profiles (repeat_level);
CREATE INDEX ix_buyer_profiles_tenant_id ON buyer_profiles (tenant_id);

CREATE TABLE conversations (
	tenant_id VARCHAR NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	channel VARCHAR(32) NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	takeover_status VARCHAR(16) NOT NULL, 
	takeover_by VARCHAR(64), 
	last_message_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_conversations_tenant_id ON conversations (tenant_id);
CREATE INDEX ix_conversations_user ON conversations (tenant_id, user_id, updated_at);
CREATE INDEX ix_conversations_user_id ON conversations (user_id);

CREATE TABLE evidence_objects (
	tenant_id VARCHAR NOT NULL, 
	owner_id VARCHAR(64) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	object_key VARCHAR(255) NOT NULL, 
	file_name VARCHAR(255) NOT NULL, 
	content_type VARCHAR(128) NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	extracted JSONB, 
	case_id UUID, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_evidence_objects_owner_id ON evidence_objects (owner_id);
CREATE INDEX ix_evidence_objects_tenant_id ON evidence_objects (tenant_id);
CREATE INDEX ix_evidence_owner ON evidence_objects (tenant_id, owner_id);

CREATE TABLE inventory_snapshots (
	tenant_id VARCHAR NOT NULL, 
	shop_id VARCHAR(64) NOT NULL, 
	sku_id VARCHAR(96) NOT NULL, 
	available_qty INTEGER NOT NULL, 
	source_version INTEGER NOT NULL, 
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id UUID NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_inventory_tenant_sku UNIQUE (tenant_id, sku_id)
)

;
CREATE INDEX ix_inventory_snapshots_shop_id ON inventory_snapshots (shop_id);
CREATE INDEX ix_inventory_snapshots_sku_id ON inventory_snapshots (sku_id);
CREATE INDEX ix_inventory_snapshots_tenant_id ON inventory_snapshots (tenant_id);

CREATE TABLE jobs (
	tenant_id VARCHAR NOT NULL, 
	job_id VARCHAR(64) NOT NULL, 
	job_type VARCHAR(64) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	payload JSONB, 
	result JSONB, 
	error TEXT, 
	attempts INTEGER NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	next_retry_at TIMESTAMP WITH TIME ZONE, 
	locked_by VARCHAR(64), 
	locked_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_jobs_tenant_job UNIQUE (tenant_id, job_id)
)

;
CREATE INDEX ix_jobs_job_type ON jobs (job_type);
CREATE INDEX ix_jobs_status ON jobs (status);
CREATE INDEX ix_jobs_status_retry ON jobs (status, next_retry_at);
CREATE INDEX ix_jobs_tenant_id ON jobs (tenant_id);
CREATE INDEX ix_jobs_type ON jobs (tenant_id, job_type);

CREATE TABLE orders (
	tenant_id VARCHAR NOT NULL, 
	shop_id VARCHAR NOT NULL, 
	platform VARCHAR(32) NOT NULL, 
	order_no VARCHAR(64) NOT NULL, 
	buyer_id VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	paid_at TIMESTAMP WITH TIME ZONE, 
	paid_amount_minor BIGINT NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	shipped_at TIMESTAMP WITH TIME ZONE, 
	delivered_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_orders_tenant_id UNIQUE (tenant_id, id), 
	CONSTRAINT uq_orders_tenant_platform_no UNIQUE (tenant_id, platform, order_no)
)

;
CREATE INDEX ix_orders_buyer_id ON orders (buyer_id);
CREATE INDEX ix_orders_buyer_paid ON orders (tenant_id, buyer_id, paid_at);
CREATE INDEX ix_orders_shop_id ON orders (shop_id);
CREATE INDEX ix_orders_status ON orders (status);
CREATE INDEX ix_orders_tenant_id ON orders (tenant_id);

CREATE TABLE outbox_events (
	tenant_id VARCHAR NOT NULL, 
	event_id VARCHAR(64) NOT NULL, 
	aggregate_type VARCHAR(64) NOT NULL, 
	aggregate_id VARCHAR(64) NOT NULL, 
	aggregate_version INTEGER NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	payload JSONB, 
	status VARCHAR(16) NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_outbox_tenant_event UNIQUE (tenant_id, event_id)
)

;
CREATE INDEX ix_outbox_events_aggregate_id ON outbox_events (aggregate_id);
CREATE INDEX ix_outbox_events_tenant_id ON outbox_events (tenant_id);
CREATE INDEX ix_outbox_status ON outbox_events (status, created_at);

CREATE TABLE policy_documents (
	tenant_id VARCHAR NOT NULL, 
	doc_id VARCHAR(64) NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	scope VARCHAR(32) NOT NULL, 
	source VARCHAR(255) NOT NULL, 
	description TEXT NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_policy_docs_tenant_doc UNIQUE (tenant_id, doc_id)
)

;
CREATE INDEX ix_policy_documents_scope ON policy_documents (scope);
CREATE INDEX ix_policy_documents_tenant_id ON policy_documents (tenant_id);

CREATE TABLE products (
	tenant_id VARCHAR NOT NULL, 
	shop_id VARCHAR NOT NULL, 
	product_id VARCHAR(64) NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	brand VARCHAR(64) NOT NULL, 
	category VARCHAR(64) NOT NULL, 
	sub_category VARCHAR(64) NOT NULL, 
	description TEXT NOT NULL, 
	attributes JSONB, 
	status VARCHAR(32) NOT NULL, 
	image_key VARCHAR(255), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_products_tenant_pid UNIQUE (tenant_id, product_id)
)

;
CREATE INDEX ix_products_brand ON products (brand);
CREATE INDEX ix_products_category ON products (category);
CREATE INDEX ix_products_shop_id ON products (shop_id);
CREATE INDEX ix_products_status ON products (status);
CREATE INDEX ix_products_tenant_id ON products (tenant_id);

CREATE TABLE reviews (
	tenant_id VARCHAR NOT NULL, 
	shop_id VARCHAR(64) NOT NULL, 
	product_id VARCHAR(64) NOT NULL, 
	rating INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	buyer_ref VARCHAR(32) NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_reviews_product ON reviews (tenant_id, product_id);
CREATE INDEX ix_reviews_shop_id ON reviews (shop_id);
CREATE INDEX ix_reviews_tenant_id ON reviews (tenant_id);

CREATE TABLE shops (
	tenant_id VARCHAR NOT NULL, 
	shop_id VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	platform VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_shops_tenant_shop UNIQUE (tenant_id, shop_id)
)

;
CREATE INDEX ix_shops_tenant_id ON shops (tenant_id);

CREATE TABLE tool_calls (
	tenant_id VARCHAR NOT NULL, 
	trace_id VARCHAR(64) NOT NULL, 
	agent VARCHAR(32) NOT NULL, 
	tool_name VARCHAR(64) NOT NULL, 
	actor_id VARCHAR(64) NOT NULL, 
	actor_scope JSONB, 
	input_summary JSONB, 
	result_summary JSONB, 
	status VARCHAR(16) NOT NULL, 
	duration_ms INTEGER NOT NULL, 
	id UUID NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_tool_calls_agent ON tool_calls (agent);
CREATE INDEX ix_tool_calls_tenant_id ON tool_calls (tenant_id);
CREATE INDEX ix_tool_calls_trace ON tool_calls (trace_id);

CREATE TABLE users (
	tenant_id VARCHAR NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	display_name VARCHAR(64) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	shop_ids JSONB, 
	is_active BOOLEAN NOT NULL, 
	last_login_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (email)
)

;
CREATE INDEX ix_users_role ON users (role);
CREATE INDEX ix_users_tenant_id ON users (tenant_id);

CREATE TABLE messages (
	tenant_id VARCHAR NOT NULL, 
	conversation_id UUID NOT NULL, 
	seq INTEGER NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	meta JSONB, 
	client_message_id VARCHAR(64), 
	trace_id VARCHAR(64), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_messages_conv_seq UNIQUE (conversation_id, seq), 
	CONSTRAINT uq_messages_client_id UNIQUE (conversation_id, client_message_id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id)
)

;
CREATE INDEX ix_messages_conv ON messages (conversation_id, seq);
CREATE INDEX ix_messages_tenant_id ON messages (tenant_id);
CREATE INDEX ix_messages_trace_id ON messages (trace_id);

CREATE TABLE order_items (
	tenant_id VARCHAR NOT NULL, 
	order_id UUID NOT NULL, 
	sku_id VARCHAR(96) NOT NULL, 
	product_id VARCHAR(64) NOT NULL, 
	quantity INTEGER NOT NULL, 
	paid_amount_minor BIGINT NOT NULL, 
	title_snapshot VARCHAR(255) NOT NULL, 
	brand_snapshot VARCHAR(64) NOT NULL, 
	category_snapshot VARCHAR(64) NOT NULL, 
	specs_snapshot JSONB, 
	id UUID NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_order_items_tenant_id UNIQUE (tenant_id, id), 
	FOREIGN KEY(tenant_id, order_id) REFERENCES orders (tenant_id, id)
)

;
CREATE INDEX ix_order_items_category_snapshot ON order_items (category_snapshot);
CREATE INDEX ix_order_items_order ON order_items (order_id);
CREATE INDEX ix_order_items_product_id ON order_items (product_id);
CREATE INDEX ix_order_items_sku_id ON order_items (sku_id);
CREATE INDEX ix_order_items_tenant_id ON order_items (tenant_id);

CREATE TABLE policy_versions (
	tenant_id VARCHAR NOT NULL, 
	policy_document_id UUID NOT NULL, 
	scope VARCHAR(32) NOT NULL, 
	version VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	effective_from TIMESTAMP WITH TIME ZONE NOT NULL, 
	effective_to TIMESTAMP WITH TIME ZONE, 
	checksum VARCHAR(64) NOT NULL, 
	index_version VARCHAR(64), 
	published_at TIMESTAMP WITH TIME ZONE, 
	published_by VARCHAR(64), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_policy_versions_doc_ver UNIQUE (policy_document_id, version), 
	FOREIGN KEY(policy_document_id) REFERENCES policy_documents (id)
)

;
CREATE INDEX ix_policy_versions_scope ON policy_versions (scope);
CREATE INDEX ix_policy_versions_status ON policy_versions (status);
CREATE INDEX ix_policy_versions_tenant_scope_status ON policy_versions (tenant_id, scope, status);
CREATE INDEX ix_policy_versions_tenant_id ON policy_versions (tenant_id);

CREATE TABLE product_skus (
	tenant_id VARCHAR NOT NULL, 
	shop_id VARCHAR NOT NULL, 
	product_id VARCHAR(64) NOT NULL, 
	sku_id VARCHAR(96) NOT NULL, 
	specs JSONB, 
	price_minor BIGINT NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_skus_tenant_sid UNIQUE (tenant_id, sku_id), 
	CONSTRAINT fk_skus_tenant_product FOREIGN KEY(tenant_id, product_id) REFERENCES products (tenant_id, product_id)
)

;
CREATE INDEX ix_product_skus_shop_id ON product_skus (shop_id);
CREATE INDEX ix_product_skus_status ON product_skus (status);
CREATE INDEX ix_product_skus_tenant_id ON product_skus (tenant_id);

CREATE TABLE after_sale_cases (
	tenant_id VARCHAR NOT NULL, 
	shop_id VARCHAR NOT NULL, 
	case_no VARCHAR(64) NOT NULL, 
	order_id UUID NOT NULL, 
	order_item_id UUID NOT NULL, 
	buyer_id VARCHAR(64) NOT NULL, 
	case_type VARCHAR(32) NOT NULL, 
	reason VARCHAR(512) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	policy_version VARCHAR(64), 
	version INTEGER NOT NULL, 
	external_case_id VARCHAR(64), 
	actor_id VARCHAR(64) NOT NULL, 
	operation VARCHAR(32) NOT NULL, 
	idempotency_key VARCHAR(128) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	draft JSONB, 
	evidence_ids JSONB, 
	reviewer_id VARCHAR(64), 
	review_comment TEXT, 
	reviewed_at TIMESTAMP WITH TIME ZONE, 
	last_error TEXT, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id, order_id) REFERENCES orders (tenant_id, id), 
	FOREIGN KEY(tenant_id, order_item_id) REFERENCES order_items (tenant_id, id), 
	CONSTRAINT uq_cases_tenant_no UNIQUE (tenant_id, case_no), 
	CONSTRAINT uq_cases_idempotency UNIQUE (tenant_id, actor_id, operation, idempotency_key)
)

;
CREATE INDEX ix_after_sale_cases_buyer_id ON after_sale_cases (buyer_id);
CREATE INDEX ix_after_sale_cases_shop_id ON after_sale_cases (shop_id);
CREATE INDEX ix_after_sale_cases_status ON after_sale_cases (status);
CREATE INDEX ix_after_sale_cases_tenant_id ON after_sale_cases (tenant_id);
CREATE INDEX ix_cases_shop_status ON after_sale_cases (shop_id, status);
CREATE INDEX ix_cases_status_updated ON after_sale_cases (tenant_id, status, updated_at);

CREATE TABLE policy_chunks (
	tenant_id VARCHAR NOT NULL, 
	policy_version_id UUID NOT NULL, 
	chunk_index INTEGER NOT NULL, 
	section_title VARCHAR(255) NOT NULL, 
	content TEXT NOT NULL, 
	char_start INTEGER NOT NULL, 
	char_end INTEGER NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	model_version VARCHAR(64) NOT NULL, 
	embedding VECTOR(1024), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(policy_version_id) REFERENCES policy_versions (id)
)

;
CREATE INDEX ix_policy_chunks_hash ON policy_chunks (content_hash);
CREATE INDEX ix_policy_chunks_tenant_id ON policy_chunks (tenant_id);
CREATE INDEX ix_policy_chunks_version ON policy_chunks (policy_version_id);

CREATE TABLE refund_items (
	tenant_id VARCHAR NOT NULL, 
	order_item_id UUID NOT NULL, 
	refund_qty INTEGER NOT NULL, 
	refund_amount_minor BIGINT NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	reason VARCHAR(255) NOT NULL, 
	id UUID NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id, order_item_id) REFERENCES order_items (tenant_id, id)
)

;
CREATE INDEX ix_refund_items_completed ON refund_items (tenant_id, completed_at);
CREATE INDEX ix_refund_items_item ON refund_items (order_item_id);
CREATE INDEX ix_refund_items_tenant_id ON refund_items (tenant_id);
