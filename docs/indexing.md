
This file includes the learning of concepts and theory of things. 

# Indexing in sqlite
Indexing allows to skip row level searching and creates `B-Tree` of indexes and store that as a separate data (using extra memory). It makes searching from O(n) to O(logn). So say have `1,67,77,216`(n) data entries, searching through them in worst case would take `n` times but using indexing the search could be reduced to `24` tries.
The only downside to this would be some extra storage and write-heavy due to extra storage. Which is not actually an issue in most cases.

### Indexes speed these operations:
- WHERE filters
- JOIN conditions
- ORDER BY and GROUP BY (data is already sorted)
- MIN/MAX lookups
- Uniqueness enforcement (UNIQUE, PRIMARY KEY)

### When to use
- Use indexing for read-heavy and large databases with low cardinaliaty (distict values).

### How
``` sql
-- Basic index
CREATE INDEX idx_users_email ON users(email);

-- Unique index
CREATE UNIQUE INDEX idx_users_email_uq ON users(email);

-- Composite (multi-column) index
CREATE INDEX idx_orders_user_date ON orders(user_id, created_at);

-- Partial index (PostgreSQL): index only a subset of rows
CREATE INDEX idx_active_users ON users(email) WHERE is_active = true;

-- Expression index (PostgreSQL)
CREATE INDEX idx_users_lower_email ON users(LOWER(email));

-- Drop an index
DROP INDEX idx_users_email;

-- Check whether the index is used
EXPLAIN ANALYZE SELECT * FROM users WHERE email = 'a@b.com';
```
### Checks
Clustered vs non-clustere
- Clustered is for uniques key that are stored in the main table (primary keys). for most of the databases (postgresql)
- Non clustered will be sepate indexes as we are creating. 

