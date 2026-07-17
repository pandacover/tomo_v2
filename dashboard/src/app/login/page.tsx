import { LoginForm } from '@/components/login-form';
import Image from 'next/image';
import { hero } from '@/lib/landing-content';

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const params = await searchParams;

  return (
    <section className="login-page">
      <div className="login-frame">
        <Image className="login-image" src={hero.image} alt="" fill preload sizes="100vw" />
        <div className="login-scrim" />
        <div className="login-form-layer">
          <LoginForm next={params.next ?? '/api/onboarding/telegram'} />
        </div>
      </div>
    </section>
  );
}
