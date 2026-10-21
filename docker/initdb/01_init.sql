-- ============================================================
-- PostgreSQL 初始化脚本（首次创建数据卷时执行）
-- 1. 业务库启用 pgvector 扩展
-- 2. 创建独立分析数据库（脱敏事实表、维度表、指标汇总表）
-- 3. 创建分析专用低权限账号（无 DDL / 写入 / 跨库权限）
-- ============================================================

-- 业务库扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 独立分析数据库（与业务库同实例，独立账号）
CREATE DATABASE ecom_analytics_db;

-- 分析 Agent 专用账号：只读业务库的批准数据集，无 DDL / 写入权限
-- 表级 SELECT 授权在迁移完成后由 scripts/grant_analytics.py 执行
CREATE ROLE ecom_analytics_agent LOGIN PASSWORD 'analytics_agent123';

-- 该账号禁止创建表 / 越权操作
ALTER ROLE ecom_analytics_agent NOCREATEDB NOCREATEROLE NOSUPERUSER;