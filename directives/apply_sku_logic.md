# Directive: Apply SKU Logic

**Goal**: Update the `CF.SKU - new` field in the Zoho inventory file by assigning correct, serialized SKUs based on the "DS SKU Map".

## 1. Inputs

- **SKU Map**: `DS SKU map.xlsx`
  - Contains the logic for categories, subcategories, and SKU prefixes.
- **Inventory File**: `Zoho items Jan 2026.csv`
  - Master list of items.

## 2. Process

### Step 1: Analyze SKU Map

- Parse `DS SKU map.xlsx` to build a lookup dictionary:
  - Key: Category/Subcategory criteria (e.g., matching keywords, vendor names).
  - Value: SKU Prefix (e.g., `ANA-001` format base).

### Step 2: Process Inventory Items

- Iterate through each row in `Zoho items Jan 2026.csv`.
- **Match Item**: Use `Item Name`, `Description`, and `Vendor` to find the matching Category from Step 1.
  - _Heuristic_: If vendor is "BICON", look for Bicon-specific logic in the map.
  - _Search_: If categorizaton is ambiguous, flag for manual review (or use search tool if implemented).

### Step 3: Assign and Serialize SKUs

- Generate the SKU using the mapped prefix.
- **Serialization**: Ensure uniqueness.
  - If multiple items fall into `Category A` (Prefix `ABC`), assign `ABC-001`, `ABC-002`, `ABC-003`.
  - Maintain a counter for each prefix to ensure no duplicates.
- Populate the `CF.SKU - new` column with the result.

### Step 4: Output

- Save the result to a **new** file in `.tmp/` first for verification (e.g., `.tmp/Zoho_items_updated.csv`).
- Do not overwrite the source file until verified.

## 3. Success Criteria

- [ ] All items have a strictly formatted SKU in `CF.SKU - new`.
- [ ] No duplicate SKUs.
- [ ] Logic matches the `DS SKU map`.
