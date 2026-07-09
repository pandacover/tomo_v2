import { getAuth } from './auth';

export const forwardAuthRequest = async (request: Request): Promise<Response> =>
  (await getAuth()).handler(request);