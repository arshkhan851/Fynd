import { describe, expect, it } from "vitest";

import {
  isValidCategorySelection,
  toggleCategory,
  type CategorySelection,
} from "./category-selection";

describe("category selection", () => {
  it("supports multiple categories and selects exactly one primary", () => {
    let selection: CategorySelection = { selectedIds: [], primaryId: null };
    selection = toggleCategory(selection, 13);
    selection = toggleCategory(selection, 29);

    expect(selection).toEqual({ selectedIds: [13, 29], primaryId: 13 });
    expect(isValidCategorySelection(selection)).toBe(true);
  });

  it("moves the primary to a remaining selection when it is removed", () => {
    const selection = toggleCategory(
      { selectedIds: [13, 29], primaryId: 13 },
      13,
    );
    expect(selection).toEqual({ selectedIds: [29], primaryId: 29 });
    expect(isValidCategorySelection(selection)).toBe(true);
  });

  it("rejects a primary category that is not selected", () => {
    expect(isValidCategorySelection({ selectedIds: [13], primaryId: 29 })).toBe(false);
  });

  it("allows no categories only when there is no primary category", () => {
    expect(isValidCategorySelection({ selectedIds: [], primaryId: null })).toBe(true);
    expect(isValidCategorySelection({ selectedIds: [], primaryId: 13 })).toBe(false);
  });
});
