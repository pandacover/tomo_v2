import { LandingPage } from '../components/landing-page';
import { siteMeta } from '../lib/landing-content';

export default async function HomePage() {
  return (
    <>
      <title>{siteMeta.title}</title>
      <LandingPage />
    </>
  );
}

export const getConfig = async () => {
  return {
    render: 'static',
  } as const;
};
