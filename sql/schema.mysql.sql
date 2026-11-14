-- Schema for CATALOG_STORE=mysql (MySQL 5.7+ / 8.x, Aurora MySQL). Run once against your database.

CREATE TABLE IF NOT EXISTS products (
  product_id  VARCHAR(64)  NOT NULL PRIMARY KEY,
  name        VARCHAR(255),
  brand       VARCHAR(128),
  category    VARCHAR(128),
  description TEXT,
  price       DECIMAL(12,2),
  currency    CHAR(3),
  url         VARCHAR(1024),
  active      TINYINT(1) NOT NULL DEFAULT 1,
  attributes  JSON NULL          -- any extra product fields
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS variants (
  product_id VARCHAR(64) NOT NULL,
  sku        VARCHAR(64) NOT NULL,
  barcode    VARCHAR(64) NULL,
  color      VARCHAR(64),
  size       VARCHAR(32),
  price      DECIMAL(12,2),
  currency   CHAR(3),
  stock      JSON NULL,          -- a number, or a map per store: {"store1": 3, "store2": 0}
  attributes JSON NULL,
  PRIMARY KEY (product_id, sku),
  KEY idx_variants_barcode (barcode)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS index_registry (   -- which vector document holds each product+colour image
  product_id VARCHAR(64) NOT NULL,
  color      VARCHAR(64) NOT NULL,
  image_hash CHAR(64)    NOT NULL,
  meta       VARCHAR(512),
  doc_id     VARCHAR(128) NOT NULL,
  PRIMARY KEY (product_id, color)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
