export type CategorySelection = {
  selectedIds: number[];
  primaryId: number | null;
};

export function toggleCategory(
  selection: CategorySelection,
  categoryId: number,
): CategorySelection {
  if (!Number.isSafeInteger(categoryId) || categoryId < 1) return selection;

  if (selection.selectedIds.includes(categoryId)) {
    const selectedIds = selection.selectedIds.filter((id) => id !== categoryId);
    return {
      selectedIds,
      primaryId:
        selection.primaryId === categoryId
          ? (selectedIds[0] ?? null)
          : selection.primaryId,
    };
  }

  return {
    selectedIds: [...selection.selectedIds, categoryId],
    primaryId: selection.primaryId ?? categoryId,
  };
}

export function isValidCategorySelection(selection: CategorySelection): boolean {
  const { selectedIds, primaryId } = selection;
  if (new Set(selectedIds).size !== selectedIds.length) return false;
  if (selectedIds.some((id) => !Number.isSafeInteger(id) || id < 1)) return false;
  if (selectedIds.length === 0) return primaryId === null;
  return primaryId !== null && selectedIds.includes(primaryId);
}
