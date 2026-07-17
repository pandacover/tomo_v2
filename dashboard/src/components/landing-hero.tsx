'use client';

import { hero } from '@/lib/landing-content';
import Image from 'next/image';
import Link from 'next/link';
import { motion, useReducedMotion, useScroll, useTransform } from 'motion/react';

export const LandingHero = () => {
  const reduceMotion = useReducedMotion();
  const { scrollY } = useScroll();
  const contentOpacity = useTransform(scrollY, [0, 340, 640], reduceMotion ? [1, 1, 1] : [1, 1, 0]);
  const contentY = useTransform(scrollY, [0, 340, 640], reduceMotion ? [0, 0, 0] : [0, 0, -52]);

  return (
    <section className="landing-hero" aria-labelledby="hero-heading">
      <div className="landing-hero-media" aria-hidden="true">
        <Image className="landing-hero-image" src={hero.image} alt="" fill preload sizes="100vw" />
      </div>
      <div className="landing-hero-scrim" />
      <motion.div className="landing-hero-content" style={{ opacity: contentOpacity, y: contentY }}>
        <p className="landing-wordmark">tomo</p>
        <div className="landing-hero-copy">
          <p className="landing-eyebrow">a private assistant</p>
          <h1 id="hero-heading" className="landing-title">
            let the day <span>stay with you.</span>
          </h1>
          <p className="landing-supporting-copy">tomo gives the thoughts you choose to keep a place to continue, without asking you to turn your life into a system.</p>
          <Link className="landing-action" href="/login?next=/api/onboarding/telegram">
            {hero.cta}
          </Link>
        </div>
      </motion.div>
    </section>
  );
};
