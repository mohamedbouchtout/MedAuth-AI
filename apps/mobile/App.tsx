import { StatusBar } from 'expo-status-bar';
import { View, StyleSheet } from 'react-native';

import { useInboundLaunch } from './src/launch/useInboundLaunch';
import { LaunchFlow } from './src/screens/LaunchFlow';

/**
 * Root.
 *
 * The whole flow lives in `LaunchFlow`, which is a component rather than inline
 * here so it can be driven in a test with a fake browser and a fake service.
 * What is genuinely this file's is the one thing a test cannot supply: whether
 * the operating system handed this app an EHR-initiated launch when it opened.
 *
 * The named `LAUNCH_ID` constant that stood here until TASK-025c is gone. It
 * was null in every build, and it was the single remaining reason this app
 * could identify nobody; the launch it stood in for is now obtained rather than
 * configured.
 */
export default function App() {
  const inbound = useInboundLaunch();

  return (
    <View style={styles.root}>
      <LaunchFlow inbound={inbound} />
      <StatusBar style="auto" />
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
  },
});
