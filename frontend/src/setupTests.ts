import '@testing-library/jest-dom/vitest'

window.matchMedia = (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as unknown as MediaQueryList
