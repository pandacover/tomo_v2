import { describe, expect, test } from 'bun:test';
import { deliverResetPasswordEmail } from './reset-password';

describe('password reset contract', () => {
  test('delivers the Better Auth callback URL through the Infra reset template', async () => {
    const messages: unknown[] = [];
    await deliverResetPasswordEmail(
      { send: async (message) => { messages.push(message); return { success: true }; } },
      {
        user: { email: 'owner@example.com', name: 'owner' },
        url: 'https://tomo.example/api/auth/reset-password/token?callbackURL=%2Freset-password',
      },
    );

    expect(messages).toEqual([{
      template: 'reset-password',
      to: 'owner@example.com',
      variables: {
        resetLink: 'https://tomo.example/api/auth/reset-password/token?callbackURL=%2Freset-password',
        userEmail: 'owner@example.com',
        userName: 'owner',
        appName: 'tomo',
      },
    }]);
  });

  test('fails closed when Infra does not accept the reset email', async () => {
    expect(deliverResetPasswordEmail(
      { send: async () => ({ success: false }) },
      { user: { email: 'owner@example.com' }, url: 'https://tomo.example/reset' },
    )).rejects.toThrow('reset email delivery failed');
  });
});
