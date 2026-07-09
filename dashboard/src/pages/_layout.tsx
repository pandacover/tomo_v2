import '../styles.css';

import type { ReactNode } from 'react';
import { SiteHeader } from '../components/site-header';
import { siteMeta } from '../lib/landing-content';

type RootLayoutProps = { children: ReactNode };

export default async function RootLayout({ children }: RootLayoutProps) {
  return (
    <div>
      <meta name="description" content={siteMeta.description} />
      <link rel="icon" type="image/png" href={siteMeta.icon} />
      <SiteHeader />
      <main>{children}</main>
    </div>
  );
}

export const getConfig = async () => {
  return {
    render: 'static',
  } as const;
};
