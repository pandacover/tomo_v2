import { LoginForm } from '@/components/login-form';

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const params = await searchParams;

  return (
    <section className="mx-auto max-w-tomo-container px-tomo-gutter py-24">
      <LoginForm next={params.next ?? '/api/onboarding/telegram'} />
    </section>
  );
}
