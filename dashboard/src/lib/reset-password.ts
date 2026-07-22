type ResetEmailSender = {
  send: (message: {
    template: 'reset-password';
    to: string;
    variables: {
      resetLink: string;
      userEmail: string;
      userName?: string;
      appName: string;
    };
  }) => Promise<{ success: boolean }>;
};

export async function deliverResetPasswordEmail(
  sender: ResetEmailSender,
  input: { user: { email: string; name?: string | null }; url: string },
): Promise<void> {
  const result = await sender.send({
    template: 'reset-password',
    to: input.user.email,
    variables: {
      resetLink: input.url,
      userEmail: input.user.email,
      userName: input.user.name ?? undefined,
      appName: 'tomo',
    },
  });
  if (!result.success) throw new Error('reset email delivery failed');
}
