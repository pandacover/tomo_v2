import { getAuth } from './auth';
import { toBetterAuthRequestFromRaw } from './auth-request';

export const forwardAuthRequest = async (request: Request): Promise<Response> =>
  (await getAuth()).handler(toBetterAuthRequestFromRaw(request));