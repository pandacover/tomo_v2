import { LoginForm } from '../components/login-form';

export default async function LoginPage({ searchParams }: { searchParams?: { next?: string } }) {
  return (
    <section className="mx-auto max-w-tomo-container px-tomo-gutter py-24">
      <LoginForm next={searchParams?.next ?? '/api/onboarding/telegram'} />
    </section>
  );
}

export const getConfig = async () => ({ render: 'dynamic' }) as const;
