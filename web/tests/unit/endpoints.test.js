import { describe, it, expect } from 'vitest';

// api/endpoints.js — GENERATED path constants (do not hand-edit source;
// regenerate via scripts/gen-api-endpoints.py). Tests pin the contract
// shape so a bad regen fails loudly.
const E = globalThis.BookqApiEndpoints;

describe('BookqApiEndpoints', () => {
  it('every endpoint is a /api/v1/ path', () => {
    for (const [k, v] of Object.entries(E)) {
      expect(v, k).toMatch(/^\/api\/v1\//);
    }
  });

  it('covers the documented surface', () => {
    expect(E).toEqual({
      authToken: '/api/v1/auth/token',
      facets: '/api/v1/facets',
      folderVisibility: '/api/v1/folder-visibility',
      guestRedeem: '/api/v1/guest/redeem',
      health: '/api/v1/health',
      images: '/api/v1/images',
      indexRefresh: '/api/v1/index/refresh',
      libraryFiles: '/api/v1/library/files',
      libraryUploads: '/api/v1/library/uploads',
      libraryUpload: '/api/v1/library/uploads/{id}',
      libraryUploadFinalize: '/api/v1/library/uploads/{id}/finalize',
      readingPositions: '/api/v1/reading-positions',
      search: '/api/v1/search',
      thumbSprite: '/api/v1/thumb-sprite',
      tree: '/api/v1/tree',
      version: '/api/v1/version',
      pairReport: '/api/v1/pair-report',
    });
  });
});
