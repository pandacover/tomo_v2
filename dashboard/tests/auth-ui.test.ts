import { describe, expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';

const login = readFileSync(new URL('../src/components/login-form.tsx', import.meta.url), 'utf8');
const relationships = readFileSync(new URL('../src/components/tomo-relationships.tsx', import.meta.url), 'utf8');
const resetPage = readFileSync(new URL('../src/app/reset-password/page.tsx', import.meta.url), 'utf8');

describe('dashboard auth UI contract', () => {
  test('uses CTA copy without routing the login action through Telegram', () => {
    expect(login).not.toContain('continue to telegram');
    expect(login).toContain("mode === 'signup' ? 'create my tomo' : 'continue'");
    expect(login).toContain('forgot password?');
    expect(login).toContain('window.location.assign(callbackURL)');
    expect(login).toContain("!next.startsWith('//')");
  });

  test('offers reset-password flows both before and after authentication', () => {
    expect(login).toContain('requestPasswordReset');
    expect(relationships).toContain('account security');
    expect(relationships).toContain('requestPasswordReset');
    expect(resetPage).toContain('authClient.resetPassword');
    expect(resetPage).toContain('confirm password');
  });
});