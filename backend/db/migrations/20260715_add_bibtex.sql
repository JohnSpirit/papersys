-- Migration: 为 papers 表新增 bibtex 字段（BibTeX 引用条目）。
-- 适用已有库；新库通过 schema.sql 的 CREATE TABLE IF NOT EXISTS 自动包含。
--
-- 幂等：SQLite 不支持 ADD COLUMN IF NOT EXISTS，重复执行会报错。
-- 已有库执行一次即可；或通过 `paper db backfill_bibtex` 命令（Python pragma 检查，幂等）。
ALTER TABLE papers ADD COLUMN bibtex TEXT;
