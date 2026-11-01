// `Money` sets the currency in its own element so it can recede, which plain
// text matching cannot see across.
export function moneyText(text: string) {
  return (_: string, element: Element | null) =>
    element?.textContent === text && (element.classList.contains('money-amount') || element.children.length === 0)
}
